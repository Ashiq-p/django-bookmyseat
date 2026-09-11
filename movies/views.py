from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required, user_passes_test
from datetime import datetime, timedelta
import base64
import csv
import hashlib
import hmac
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, ExpressionWrapper, F, IntegerField, Q, Subquery, Sum, Value, When
from django.db.models.functions import ExtractHour, TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.http import FileResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
import logging

from .models import (
    Booking, Movie, MoviePoster, Seat, SeatReservation, Payment, PaymentWebhookEvent, Theater, Genre, Language,
    CastMember, MovieView, Review, ReviewReport
)

User = get_user_model()
logger = logging.getLogger(__name__)

def staff_check(user):
    return user.is_authenticated and user.is_staff


def analytics_access(view):
    """Require both staff status and Django's payment view permission."""
    return user_passes_test(staff_check)(permission_required('movies.view_payment', raise_exception=True)(view))


def analytics_date_range(request):
    today = timezone.localdate()
    default_start = today - timedelta(days=29)
    try:
        start = datetime.strptime(request.GET.get('start_date', ''), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        start = default_start
    try:
        end = datetime.strptime(request.GET.get('end_date', ''), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        end = today
    if start > end:
        start, end = end, start
    return start, end, timezone.make_aware(datetime.combine(end + timedelta(days=1), datetime.min.time()))


def analytics_context(request):
    start, end, end_exclusive = analytics_date_range(request)
    start_at = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    booking_filter = Q(booked_at__gte=start_at, booked_at__lt=end_exclusive)
    payment_filter = Q(created_at__gte=start_at, created_at__lt=end_exclusive)

    bookings = Booking.objects.filter(booking_filter)
    payments = Payment.objects.filter(payment_filter)
    paid_payments = payments.filter(status='paid')

    revenue = paid_payments.aggregate(total=Sum('amount'))['total'] or 0
    period_boundaries = {
        'daily': timezone.localdate(),
        'weekly': timezone.localdate() - timedelta(days=timezone.localdate().weekday()),
        'monthly': timezone.localdate().replace(day=1),
        'yearly': timezone.localdate().replace(month=1, day=1),
    }
    revenue_periods = {}
    for name, period_start in period_boundaries.items():
        period_end = timezone.localdate() + timedelta(days=1)
        period_total = Payment.objects.filter(
            status='paid', created_at__gte=timezone.make_aware(datetime.combine(period_start, datetime.min.time())),
            created_at__lt=timezone.make_aware(datetime.combine(period_end, datetime.min.time())),
        ).aggregate(total=Sum('amount'))['total'] or 0
        revenue_periods[name] = period_total

    booking_trends = list(bookings.annotate(day=TruncDate('booked_at')).values('day').annotate(
        bookings=Count('id')
    ).order_by('day'))
    occupancy = list(
        Theater.objects.annotate(
            total_seats=Count('seats', distinct=True),
            booked_seats=Count(
                'seats__booking',
                filter=Q(seats__booking__booked_at__gte=start_at, seats__booking__booked_at__lt=end_exclusive),
                distinct=True,
            ),
        )
        .values('id', 'name', 'total_seats', 'booked_seats')
        .order_by('-booked_seats', 'name')
    )
    for theater in occupancy:
        theater['occupancy_percentage'] = round(
            theater['booked_seats'] * 100 / theater['total_seats'], 2
        ) if theater['total_seats'] else 0

    top_movies = list(bookings.values('movie__name').annotate(bookings=Count('id')).order_by('-bookings', 'movie__name')[:10])
    top_theaters = list(bookings.values('theater__name').annotate(bookings=Count('id')).order_by('-bookings', 'theater__name')[:10])
    peak_hours = list(bookings.annotate(hour=ExtractHour('booked_at')).values('hour').annotate(
        bookings=Count('id')
    ).order_by('-bookings', 'hour')[:24])
    payment_stats = payments.aggregate(
        cancelled=Count('id', filter=Q(status='cancelled')),
        failed=Count('id', filter=Q(status='failed')),
        refunded=Count('id', filter=Q(status='refunded')),
        refund_amount=Sum('refund_amount', filter=Q(status='refunded')),
    )
    user_growth = list(User.objects.filter(date_joined__gte=start_at, date_joined__lt=end_exclusive).annotate(
        day=TruncDate('date_joined')
    ).values('day').annotate(users=Count('id')).order_by('day'))

    return {
        'analytics_start': start,
        'analytics_end': end,
        'revenue': revenue,
        'revenue_rupees': revenue / 100,
        'revenue_periods': revenue_periods,
        'revenue_periods_rupees': {name: amount / 100 for name, amount in revenue_periods.items()},
        'total_bookings': bookings.count(),
        'booking_trends': booking_trends,
        'occupancy': occupancy,
        'top_movies': top_movies,
        'top_theaters': top_theaters,
        'peak_hours': peak_hours,
        'payment_stats': payment_stats,
        'refund_amount_rupees': (payment_stats['refund_amount'] or 0) / 100,
        'user_growth': user_growth,
    }


MOVIE_SORT_EXPRESSIONS = {
    'popularity': ('-popularity', 'name'),
    'newest': ('-release_date', 'name'),
    'rating': ('-rating', 'name'),
    'price': ('-id',),
}


def movie_discovery_queryset(params):
    search_query = params.get('search', '').strip()
    selected_genre = params.get('genre', '').strip()
    selected_language = params.get('language', '').strip()
    selected_city = params.get('city', '').strip()
    selected_theater = params.get('theater', '').strip()
    selected_rating = params.get('min_rating', '').strip()
    selected_sort = params.get('sort', 'newest').strip()
    selected_release_from = params.get('release_from', '').strip()
    selected_release_to = params.get('release_to', '').strip()
    selected_show_date = params.get('show_date', '').strip()
    selected_show_time_from = params.get('show_time_from', '').strip()
    selected_show_time_to = params.get('show_time_to', '').strip()

    movies = Movie.objects.prefetch_related(
        'genres', 'languages', 'cast_members'
    )

    if search_query:
        movies = movies.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(cast__icontains=search_query) |
            Q(cast_members__name__icontains=search_query)
        )

    if selected_genre:
        movies = movies.filter(genres__slug=selected_genre)

    if selected_language:
        movies = movies.filter(languages__code=selected_language)

    if selected_city:
        movies = movies.filter(theaters__location=selected_city)

    if selected_theater.isdigit():
        movies = movies.filter(theaters__id=int(selected_theater))

    try:
        release_from = datetime.strptime(selected_release_from, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        release_from = None
    if release_from:
        movies = movies.filter(release_date__gte=release_from)

    try:
        release_to = datetime.strptime(selected_release_to, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        release_to = None
    if release_to:
        movies = movies.filter(release_date__lte=release_to)

    try:
        minimum_rating = float(selected_rating)
    except (TypeError, ValueError):
        minimum_rating = None
    if minimum_rating is not None and 0 <= minimum_rating <= 5:
        movies = movies.filter(rating__gte=minimum_rating)

    try:
        show_date = datetime.strptime(selected_show_date, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        show_date = None
    if show_date:
        movies = movies.filter(theaters__time__date=show_date)

    try:
        show_time_from = datetime.strptime(selected_show_time_from, '%H:%M').time()
    except (TypeError, ValueError):
        show_time_from = None
    if show_time_from:
        movies = movies.filter(theaters__time__time__gte=show_time_from)

    try:
        show_time_to = datetime.strptime(selected_show_time_to, '%H:%M').time()
    except (TypeError, ValueError):
        show_time_to = None
    if show_time_to:
        movies = movies.filter(theaters__time__time__lte=show_time_to)

    selected_sort = selected_sort if selected_sort in MOVIE_SORT_EXPRESSIONS else 'newest'
    if selected_sort == 'price':
        movies = movies.order_by(*MOVIE_SORT_EXPRESSIONS[selected_sort])
    else:
        movies = movies.annotate(popularity=Count('booking', distinct=True)).order_by(
            *MOVIE_SORT_EXPRESSIONS[selected_sort]
        )

    return movies.distinct()


def movie_list(request):
    movies_queryset = movie_discovery_queryset(request.GET)
    paginator = Paginator(movies_queryset, 12)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)
    genres = Genre.objects.order_by('name')
    languages = Language.objects.order_by('name')
    cities = Theater.objects.exclude(location='').values_list('location', flat=True).distinct().order_by('location')
    theaters = Theater.objects.select_related('movie').order_by('name', 'location', 'time')
    selected_sort = request.GET.get('sort', 'newest')
    if selected_sort not in MOVIE_SORT_EXPRESSIONS:
        selected_sort = 'newest'

    context = {
        'movies': page_obj,
        'page_obj': page_obj,
        'movie_count': paginator.count,
        'pagination_query': query_params.urlencode(),
        'genres': genres,
        'languages': languages,
        'cities': cities,
        'theaters': theaters,
        'search_query': request.GET.get('search', '').strip(),
        'selected_genre': request.GET.get('genre', '').strip(),
        'selected_language': request.GET.get('language', '').strip(),
        'selected_city': request.GET.get('city', '').strip(),
        'selected_theater': request.GET.get('theater', '').strip(),
        'selected_release_from': request.GET.get('release_from', '').strip(),
        'selected_release_to': request.GET.get('release_to', '').strip(),
        'selected_rating': request.GET.get('min_rating', '').strip(),
        'selected_show_date': request.GET.get('show_date', '').strip(),
        'selected_show_time_from': request.GET.get('show_time_from', '').strip(),
        'selected_show_time_to': request.GET.get('show_time_to', '').strip(),
        'selected_sort': selected_sort,
        'price_sort_available': False,
    }
    return render(request, 'movies/movie_list.html', context)


def movie_count(request):
    return JsonResponse({'count': movie_discovery_queryset(request.GET).count()})


def generic_recommendations(exclude_movie_id=None, limit=4):
    recommendations = Movie.objects.prefetch_related(
        'genres', 'languages', 'cast_members'
    ).annotate(
        booking_count=Count('booking', distinct=True),
    )
    if exclude_movie_id is not None:
        recommendations = recommendations.exclude(id=exclude_movie_id)

    trending = list(recommendations.filter(is_trending=True).order_by(
        '-booking_count', '-rating', '-release_date', 'name'
    )[:limit])
    if trending:
        return trending

    return list(recommendations.order_by(
        '-rating', '-booking_count', '-release_date', 'name'
    )[:limit])


def personalized_recommendations(user, exclude_movie_id=None, limit=4):
    if not user.is_authenticated:
        return generic_recommendations(exclude_movie_id=exclude_movie_id, limit=limit)

    booked_movie_ids = Booking.objects.filter(user=user).values('movie_id')
    recently_viewed_ids = MovieView.objects.filter(user=user).order_by(
        '-viewed_at'
    ).values('movie_id')[:5]

    if not Booking.objects.filter(user=user).exists() and not MovieView.objects.filter(user=user).exists():
        return generic_recommendations(exclude_movie_id=exclude_movie_id, limit=limit)

    booked_genre_ids = Movie.objects.filter(
        booking__user=user
    ).values('genres').distinct()
    booked_language_ids = Movie.objects.filter(
        booking__user=user
    ).values('languages').distinct()
    viewed_genre_ids = MovieView.objects.filter(
        user=user
    ).values('movie__genres').distinct()
    viewed_language_ids = MovieView.objects.filter(
        user=user
    ).values('movie__languages').distinct()

    recommendations = Movie.objects.exclude(
        id__in=Subquery(booked_movie_ids)
    ).prefetch_related(
        'genres', 'languages', 'cast_members'
    ).annotate(
        booking_count=Count('booking', distinct=True),
        booked_genre_match=Count(
            'genres',
            filter=Q(genres__id__in=Subquery(booked_genre_ids)),
            distinct=True,
        ),
        booked_language_match=Count(
            'languages',
            filter=Q(languages__id__in=Subquery(booked_language_ids)),
            distinct=True,
        ),
        viewed_genre_match=Count(
            'genres',
            filter=Q(genres__id__in=Subquery(viewed_genre_ids)),
            distinct=True,
        ),
        viewed_language_match=Count(
            'languages',
            filter=Q(languages__id__in=Subquery(viewed_language_ids)),
            distinct=True,
        ),
        recently_viewed=Case(
            When(id__in=Subquery(recently_viewed_ids), then=Value(2)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    )
    if exclude_movie_id is not None:
        recommendations = recommendations.exclude(id=exclude_movie_id)

    recommendations = recommendations.annotate(
        recommendation_score=ExpressionWrapper(
            F('booked_genre_match') * Value(5) +
            F('booked_language_match') * Value(4) +
            F('viewed_genre_match') * Value(3) +
            F('viewed_language_match') * Value(2) +
            F('recently_viewed'),
            output_field=IntegerField(),
        )
    ).order_by(
        '-recommendation_score',
        '-booking_count',
        '-rating',
        '-release_date',
        'name',
    )[:limit]

    recommendation_list = list(recommendations)
    if recommendation_list:
        return recommendation_list
    return generic_recommendations(exclude_movie_id=exclude_movie_id, limit=limit)


def movie_detail(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    if request.user.is_authenticated:
        MovieView.objects.update_or_create(
            user=request.user,
            movie=movie,
            defaults={'viewed_at': timezone.now()},
        )
    posters = movie.posters.all()
    cast_members = movie.cast_members.all()
    
    # Visible reviews (unflagged reviews + current user's review if flagged)
    review_visibility = Q(is_flagged=False)
    if request.user.is_authenticated:
        review_visibility |= Q(user=request.user)
    reviews = movie.reviews.filter(review_visibility).select_related('user').order_by('-created_at')

    user_review = None
    can_review = False
    has_booked_and_watched = False

    if request.user.is_authenticated:
        user_review = reviews.filter(user=request.user).first()
        
        # Check if user has booked and the show time has passed
        # (or any theater showtime for this movie that user booked is <= now)
        user_bookings = Booking.objects.filter(
            user=request.user,
            movie=movie,
            theater__time__lte=timezone.now()
        )
        if user_bookings.exists():
            has_booked_and_watched = True
            can_review = True

    # Recommendations logic:
    # 1. Similar Movies: match genres or languages, excluding current movie
    genre_ids = movie.genres.values_list('id', flat=True)
    language_ids = movie.languages.values_list('id', flat=True)

    similar_movies = list(Movie.objects.filter(
        Q(genres__id__in=genre_ids) | Q(languages__id__in=language_ids)
    ).exclude(id=movie.id).distinct()[:4])

    # If no exact genre match, fallback to top rated
    if not similar_movies:
        similar_movies = list(Movie.objects.exclude(id=movie.id).order_by('-rating')[:4])

    # 2. Trending Movies
    trending_movies = list(Movie.objects.filter(is_trending=True).exclude(id=movie.id)[:4])
    if not trending_movies:
        trending_movies = list(Movie.objects.exclude(id=movie.id).order_by('-rating')[:4])

    # 3. Recently Released Movies
    recent_movies = list(Movie.objects.exclude(id=movie.id).order_by('-release_date')[:4])

    personalized_movies = personalized_recommendations(
        request.user,
        exclude_movie_id=movie.id,
        limit=4,
    )

    context = {
        'movie': movie,
        'posters': posters,
        'cast_members': cast_members,
        'reviews': reviews,
        'user_review': user_review,
        'can_review': can_review,
        'has_booked_and_watched': has_booked_and_watched,
        'similar_movies': similar_movies,
        'trending_movies': trending_movies,
        'recent_movies': recent_movies,
        'personalized_movies': personalized_movies,
    }
    return render(request, 'movies/movie_detail.html', context)


@login_required(login_url='/login/')
def add_or_edit_review(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    
    # Registered users can submit ratings and reviews ONLY after booking and watching a movie
    has_booked_and_watched = Booking.objects.filter(
        user=request.user,
        movie=movie,
        theater__time__lte=timezone.now()
    ).exists()

    if not has_booked_and_watched:
        messages.error(
            request, 
            "You can submit a review only after booking a ticket and watching the movie."
        )
        return redirect('movie_detail', movie_id=movie.id)

    if request.method == 'POST':
        rating = request.POST.get('rating')
        comment = request.POST.get('comment', '').strip()

        if not rating or not comment:
            messages.error(request, "Please provide both a rating and a comment.")
            return redirect('movie_detail', movie_id=movie.id)

        try:
            rating_val = int(rating)
            if rating_val < 1 or rating_val > 5:
                raise ValueError()
        except ValueError:
            messages.error(request, "Invalid rating value.")
            return redirect('movie_detail', movie_id=movie.id)

        review, created = Review.objects.update_or_create(
            user=request.user,
            movie=movie,
            defaults={
                'rating': rating_val,
                'comment': comment,
                'is_verified_viewer': True
            }
        )

        if created:
            messages.success(request, "Your review has been published with a Verified Viewer badge!")
        else:
            messages.success(request, "Your review has been updated.")

    return redirect('movie_detail', movie_id=movie.id)


@login_required(login_url='/login/')
def report_review(request, review_id):
    review = get_object_or_404(Review, id=review_id)
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip() or "Inappropriate content reported by user."
        
        ReviewReport.objects.create(
            review=review,
            reporter=request.user,
            reason=reason
        )
        review.is_flagged = True
        review.save()
        messages.success(request, "Thank you. The review has been reported to administration for review.")

    return redirect('movie_detail', movie_id=review.movie.id)


def theater_list(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    theaters = Theater.objects.filter(movie=movie)
    return render(request, 'movies/theater_list.html', {'movie': movie, 'theaters': theaters})


RESERVATION_LENGTH = timedelta(minutes=2)


def clear_expired_reservations(theater=None):
    """Remove expired holds before evaluating availability."""
    reservations = SeatReservation.objects.filter(expires_at__lte=timezone.now())
    if theater is not None:
        reservations = reservations.filter(theater=theater)
    reservations.delete()


def razorpay_api(path, method='GET', payload=None):
    """Small server-only Razorpay client; no payment secret reaches the browser."""
    credentials = f'{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}'.encode()
    headers = {'Authorization': f'Basic {base64.b64encode(credentials).decode()}', 'Content-Type': 'application/json'}
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(f'https://api.razorpay.com/v1/{path}', data=data, headers=headers, method=method)
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def gateway_payment_matches(payment, gateway_payment):
    """Validate gateway payment facts against the server-side Payment record."""
    try:
        gateway_amount = int(gateway_payment.get('amount'))
    except (TypeError, ValueError):
        return False
    gateway_currency = str(gateway_payment.get('currency', '')).upper()
    return gateway_amount == payment.amount and gateway_currency == payment.currency.upper()


def release_payment_reservations(payment):
    SeatReservation.objects.filter(user=payment.user, theater=payment.theater, seat_id__in=payment.seat_ids).delete()


def fail_payment(payment, reason, status='failed'):
    """Move an unpaid transaction to a terminal state and release its holds."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status in ('paid', 'failed', 'cancelled', 'refunded'):
            return payment
        payment.status = status
        payment.failure_reason = reason[:500]
        payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        release_payment_reservations(payment)
        return payment


def confirm_paid_payment(payment, payment_id):
    """Idempotently convert the payment's active holds to bookings."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == 'paid':
            return payment
        if payment.status in ('failed', 'cancelled', 'refunded'):
            return payment
        if payment.razorpay_payment_id and payment.razorpay_payment_id != payment_id:
            payment.status = 'failed'
            payment.failure_reason = 'Payment ID did not match the existing payment record.'
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
            release_payment_reservations(payment)
            return payment
        clear_expired_reservations(payment.theater)
        holds = list(SeatReservation.objects.select_for_update().filter(
            user=payment.user, theater=payment.theater, seat_id__in=payment.seat_ids
        ).order_by('seat_id'))
        if len(holds) != len(payment.seat_ids):
            payment.status = 'failed'
            payment.failure_reason = 'Seat reservation expired before payment confirmation.'
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
            release_payment_reservations(payment)
            return payment
        seats = list(Seat.objects.select_for_update().filter(
            id__in=payment.seat_ids,
            theater=payment.theater,
        ).order_by('id'))
        if len(seats) != len(payment.seat_ids) or Booking.objects.filter(seat_id__in=payment.seat_ids).exists():
            payment.status = 'failed'
            payment.failure_reason = 'A seat was no longer available.'
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
            release_payment_reservations(payment)
            return payment
        for seat in seats:
            booking, created = Booking.objects.get_or_create(
                user=payment.user,
                seat=seat,
                defaults={
                    'movie': payment.theater.movie,
                    'theater': payment.theater,
                    'payment': payment,
                },
            )
            if not created and booking.payment_id is None:
                booking.payment = payment
                booking.save(update_fields=['payment'])
            seat.is_booked = True
            seat.save(update_fields=['is_booked'])
        SeatReservation.objects.filter(id__in=[hold.id for hold in holds]).delete()
        payment.status = 'paid'
        payment.razorpay_payment_id = payment_id
        payment.save(update_fields=['status', 'razorpay_payment_id', 'updated_at'])
        transaction.on_commit(lambda: _queue_ticket_task(payment.id))
        return payment


def _queue_ticket_task(payment_id):
    from .tasks import generate_and_email_ticket
    try:
        generate_and_email_ticket.delay(payment_id)
    except Exception:
        logger.exception('Ticket task could not be queued; payment remains confirmed.')


@login_required(login_url='/login/')
def download_ticket(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id, user=request.user)
    if not booking.ticket_pdf:
        return HttpResponse('Ticket is still being generated.', status=404)
    return FileResponse(booking.ticket_pdf.open('rb'), as_attachment=True, filename=f'bookmyseat-ticket-{booking.id}.pdf')


def verify_ticket(request, token):
    from django.core import signing
    try:
        payload = signing.loads(token, salt='ticket-verification', max_age=None)
        booking = Booking.objects.select_related('movie', 'theater', 'seat', 'payment').get(id=payload['booking_id'])
    except (signing.BadSignature, KeyError, TypeError, ValueError, Booking.DoesNotExist):
        return JsonResponse({'valid': False, 'message': 'Ticket is invalid.'}, status=404)
    if not booking.payment or booking.payment.status != 'paid':
        return JsonResponse({'valid': False, 'message': 'Payment is not confirmed.'}, status=400)
    seats = Booking.objects.filter(payment=booking.payment).select_related('seat').values_list('seat__seat_number', flat=True)
    return JsonResponse({
        'valid': True,
        'booking_id': booking.id,
        'movie': booking.movie.name,
        'theater': booking.theater.name,
        'show': booking.theater.time.isoformat(),
        'seats': list(seats),
    })


def confirm_prototype_booking(user, theater):
    """Convert the user's active seat holds directly into bookings."""
    with transaction.atomic():
        clear_expired_reservations(theater)
        holds = list(SeatReservation.objects.select_for_update().filter(
            user=user, theater=theater
        ).select_related('seat').order_by('seat_id'))
        if not holds:
            return []

        seat_ids = [hold.seat_id for hold in holds]
        seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids).order_by('id'))
        if (len(seats) != len(seat_ids)
            or any(seat.is_booked for seat in seats)
            or Booking.objects.filter(seat_id__in=seat_ids).exists()):
            return None

        for seat in seats:
            Booking.objects.get_or_create(
                user=user, seat=seat,
                defaults={'movie': theater.movie, 'theater': theater},
            )
            seat.is_booked = True
            seat.save(update_fields=['is_booked'])
        SeatReservation.objects.filter(id__in=[hold.id for hold in holds]).delete()
        return seats


@login_required(login_url='/login/')
def book_seat(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)
    # Repair old seat records left marked as booked after their Booking was
    # deleted in the admin before the delete signal was added.
    Seat.objects.filter(
        theater=theater, is_booked=True, booking__isnull=True
    ).update(is_booked=False)
    clear_expired_reservations(theater)

    if request.method == 'POST':
        action = request.POST.get('action', 'reserve')

        if action == 'cancel':
            SeatReservation.objects.filter(user=request.user, theater=theater).delete()
            messages.info(request, 'Your reserved seats have been released.')
            return redirect('book_seat', theater_id=theater.id)

        if action == 'complete_payment':
            return redirect('start_payment', theater_id=theater.id)

        selected_seat_ids = request.POST.getlist('seats')
        if not selected_seat_ids:
            messages.error(request, 'Select at least one seat to reserve.')
            return redirect('book_seat', theater_id=theater.id)

        try:
            with transaction.atomic():
                clear_expired_reservations(theater)
                selected_ids = sorted({int(seat_id) for seat_id in selected_seat_ids})
                locked_seats = list(
                    Seat.objects.select_for_update().filter(theater=theater, id__in=selected_ids).order_by('id')
                )
                if len(locked_seats) != len(selected_ids):
                    messages.error(request, 'An invalid seat was selected.')
                    return redirect('book_seat', theater_id=theater.id)

                unavailable = [seat.seat_number for seat in locked_seats if seat.is_booked or Booking.objects.filter(seat=seat).exists()]
                other_holds = SeatReservation.objects.filter(seat_id__in=selected_ids).exclude(user=request.user)
                unavailable.extend(other_holds.values_list('seat__seat_number', flat=True))
                if unavailable:
                    messages.error(request, f'Already reserved or booked: {", ".join(sorted(set(unavailable)))}.')
                    return redirect('book_seat', theater_id=theater.id)

                # Replacing the user's hold makes seat selection editable before payment.
                SeatReservation.objects.filter(user=request.user, theater=theater).exclude(seat_id__in=selected_ids).delete()
                SeatReservation.objects.filter(user=request.user, seat_id__in=selected_ids).update(
                    expires_at=timezone.now() + RESERVATION_LENGTH
                )
                existing_ids = set(SeatReservation.objects.filter(user=request.user, seat_id__in=selected_ids).values_list('seat_id', flat=True))
                SeatReservation.objects.bulk_create([
                    SeatReservation(user=request.user, seat=seat, theater=theater, expires_at=timezone.now() + RESERVATION_LENGTH)
                    for seat in locked_seats if seat.id not in existing_ids
                ])
        except (IntegrityError, ValueError):
            messages.error(request, 'Those seats were just reserved by another customer. Please choose again.')
            return redirect('book_seat', theater_id=theater.id)

        messages.success(request, 'Seats reserved for 2 minutes. You can modify your selection before payment.')
        return redirect('book_seat', theater_id=theater.id)

    user_reservations = SeatReservation.objects.filter(user=request.user, theater=theater)
    reserved_seat_ids = set(user_reservations.values_list('seat_id', flat=True))
    other_reserved_seat_ids = set(
        SeatReservation.objects.filter(theater=theater).exclude(user=request.user).values_list('seat_id', flat=True)
    )
    reservation_expires_at = user_reservations.order_by('expires_at').values_list('expires_at', flat=True).first()
    seats = Seat.objects.filter(theater=theater).order_by('seat_number')
    return render(request, 'movies/seat_selection.html', {
        'theater': theater, 'seats': seats, 'reserved_seat_ids': reserved_seat_ids,
        'other_reserved_seat_ids': other_reserved_seat_ids, 'reservation_expires_at': reservation_expires_at,
    })


@login_required(login_url='/login/')
def start_payment(request, theater_id):
    if request.method != 'POST':
        return redirect('book_seat', theater_id=theater_id)
    theater = get_object_or_404(Theater, id=theater_id)
    clear_expired_reservations(theater)
    reservations = list(SeatReservation.objects.filter(user=request.user, theater=theater).select_related('seat'))
    if not reservations:
        messages.error(request, 'Your reservation expired. Please select seats again.')
        return redirect('book_seat', theater_id=theater.id)
    seat_ids = sorted(reservation.seat_id for reservation in reservations)
    amount = len(seat_ids) * settings.TICKET_PRICE_PAISE
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        messages.error(request, 'Online payments are not configured yet. Please try again later.')
        return redirect('book_seat', theater_id=theater.id)
    try:
        logger.info('Creating Razorpay order for a reserved seat set.')
        order = razorpay_api('orders', method='POST', payload={
            'amount': amount,
            'currency': 'INR',
            'receipt': f'booking-{request.user.id}-{timezone.now().timestamp():.0f}',
            'notes': {'theater_id': str(theater.id), 'user_id': str(request.user.id)},
        })
        if (
            int(order.get('amount', -1)) != amount
            or str(order.get('currency', '')).upper() != 'INR'
        ):
            raise ValueError('Razorpay order amount or currency did not match the local payment.')
        payment = Payment.objects.create(
            user=request.user,
            theater=theater,
            seat_ids=seat_ids,
            amount=amount,
            currency=order.get('currency', 'INR'),
            status='pending',
            razorpay_order_id=order['id'],
        )
        logger.info('Razorpay order and local pending payment created.')
    except Exception as exc:
        logger.exception('Razorpay order creation failed.')
        messages.error(request, 'Payment service is unavailable. Your seats remain reserved briefly; please try again.')
        return redirect('book_seat', theater_id=theater.id)
    return render(request, 'movies/payment_checkout.html', {
        'payment': payment,
        'razorpay_key_id': settings.RAZORPAY_KEY_ID,
        'amount_rupees': payment.amount / 100,
    })


@login_required(login_url='/login/')
def retry_payment(request, payment_id):
    if request.method != 'POST':
        return redirect('profile')
    previous = get_object_or_404(Payment, id=payment_id, user=request.user)
    if previous.status == 'paid':
        return redirect('profile')
    theater = previous.theater
    clear_expired_reservations(theater)
    with transaction.atomic():
        seats = list(Seat.objects.select_for_update().filter(id__in=previous.seat_ids, theater=theater).order_by('id'))
        if len(seats) != len(previous.seat_ids) or any(seat.is_booked for seat in seats):
            messages.error(request, 'One or more seats are no longer available.')
            return redirect('profile')
        held_by_other = SeatReservation.objects.filter(
            seat_id__in=previous.seat_ids, theater=theater
        ).exclude(user=request.user).exists()
        if held_by_other:
            messages.error(request, 'One or more seats are currently reserved by another customer.')
            return redirect('profile')
        SeatReservation.objects.filter(user=request.user, theater=theater).delete()
        expires_at = timezone.now() + RESERVATION_LENGTH
        SeatReservation.objects.bulk_create([
            SeatReservation(user=request.user, seat=seat, theater=theater, expires_at=expires_at)
            for seat in seats
        ])
    messages.info(request, 'Seats reserved again for 2 minutes. Continue to payment to retry the transaction.')
    return redirect('book_seat', theater_id=theater.id)


@login_required(login_url='/login/')
def cancel_payment(request, payment_id):
    if request.method != 'POST':
        return redirect('profile')
    payment = get_object_or_404(Payment, id=payment_id, user=request.user)
    if payment.status in ('created', 'pending'):
        fail_payment(payment, 'Payment cancelled by customer.', status='cancelled')
    messages.info(request, 'Payment cancelled and reserved seats released.')
    return redirect('profile')


@csrf_exempt
@login_required(login_url='/login/')
def payment_callback(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')
    order_id = request.POST.get('razorpay_order_id', '')
    payment_id = request.POST.get('razorpay_payment_id', '')
    signature = request.POST.get('razorpay_signature', '')
    payment = get_object_or_404(Payment, razorpay_order_id=order_id, user=request.user)
    if payment.status in ('paid', 'refunded'):
        return redirect('profile')
    expected = hmac.new(settings.RAZORPAY_KEY_SECRET.encode(), f'{payment.razorpay_order_id}|{payment_id}'.encode(), hashlib.sha256).hexdigest()
    if not payment_id or not hmac.compare_digest(expected, signature):
        fail_payment(payment, 'Invalid Razorpay payment signature.')
        messages.error(request, 'Payment verification failed; the reserved seats were released.')
        return redirect('profile')
    try:
        gateway_payment = razorpay_api(f'payments/{payment_id}')
    except (HTTPError, URLError, TimeoutError, ValueError):
        messages.info(request, 'Payment is being verified. Your booking will appear once confirmed.')
        return redirect('profile')
    if (
        gateway_payment.get('status') != 'captured'
        or gateway_payment.get('order_id') != payment.razorpay_order_id
        or not gateway_payment_matches(payment, gateway_payment)
    ):
        fail_payment(payment, 'Payment was not captured.')
        messages.error(request, 'Payment was not captured; the reserved seats were released.')
        return redirect('profile')
    confirmed = confirm_paid_payment(payment, payment_id)
    messages.success(request, 'Payment verified and booking confirmed.' if confirmed.status == 'paid' else confirmed.failure_reason)
    return redirect('profile')


@csrf_exempt
def razorpay_webhook(request):
    if request.method != 'POST' or not settings.RAZORPAY_WEBHOOK_SECRET:
        return HttpResponseBadRequest('Invalid webhook request')
    raw_body = request.body
    expected = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get('X-Razorpay-Signature', '')):
        return HttpResponseBadRequest('Invalid webhook signature')
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')
    event_id = request.headers.get('X-Razorpay-Event-Id', '')
    event_type = payload.get('event', '')
    webhook_payload = payload.get('payload', {})
    entity = (
        webhook_payload.get('payment', {}).get('entity')
        or webhook_payload.get('order', {}).get('entity')
        or {}
    )
    if event_type == 'order.paid':
        order_id = entity.get('id')
        payment_id = webhook_payload.get('payment', {}).get('entity', {}).get('id')
    else:
        order_id, payment_id = entity.get('order_id'), entity.get('id')
    if not event_id or not order_id:
        return HttpResponseBadRequest('Incomplete webhook')
    payment = Payment.objects.filter(razorpay_order_id=order_id).first()
    if payment is None:
        return HttpResponse(status=200)
    try:
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(pk=payment.pk)
            if PaymentWebhookEvent.objects.filter(event_id=event_id).exists():
                return HttpResponse(status=200)

            if event_type in ('payment.captured', 'order.paid') and payment_id:
                if not gateway_payment_matches(payment, entity):
                    fail_payment(payment, 'Webhook payment amount or currency did not match.')
                else:
                    confirm_paid_payment(payment, payment_id)
            elif event_type in ('payment.failed', 'payment.cancelled'):
                fail_payment(
                    payment,
                    entity.get('error_description', event_type),
                    status='failed' if event_type == 'payment.failed' else 'cancelled',
                )

            PaymentWebhookEvent.objects.create(
                event_id=event_id,
                payment=payment,
                event_type=event_type,
            )
    except Exception:
        logger.exception('Razorpay webhook processing failed; event will be retried.')
        return HttpResponse(status=500)
    return HttpResponse(status=200)


# --- Custom Admin Interface Views ---

@analytics_access
def custom_admin_dashboard(request):
    total_movies = Movie.objects.count()
    total_bookings = Booking.objects.count()
    total_reviews = Review.objects.count()
    flagged_reviews_count = Review.objects.filter(is_flagged=True).count()
    
    recent_bookings = Booking.objects.select_related('user', 'movie', 'theater').order_by('-booked_at')[:5]
    flagged_reviews = Review.objects.filter(is_flagged=True).select_related('user', 'movie')[:5]

    context = {
        'total_movies': total_movies,
        'total_bookings': total_bookings,
        'total_reviews': total_reviews,
        'flagged_reviews_count': flagged_reviews_count,
        'recent_bookings': recent_bookings,
        'flagged_reviews': flagged_reviews,
    }
    context.update(analytics_context(request))
    return render(request, 'movies/custom_admin/dashboard.html', context)


@analytics_access
def export_analytics_csv(request):
    start, end, end_exclusive = analytics_date_range(request)
    start_at = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    report = request.GET.get('report', 'revenue')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="bookmyseat-{report}-{start}-to-{end}.csv"'
    writer = csv.writer(response)
    if report == 'revenue':
        writer.writerow(['Date', 'Paid revenue (paise)'])
        rows = Payment.objects.filter(status='paid', created_at__gte=start_at, created_at__lt=end_exclusive).annotate(
            day=TruncDate('created_at')
        ).values('day').annotate(revenue=Sum('amount')).order_by('day').iterator()
        for row in rows:
            writer.writerow([row['day'], row['revenue']])
    elif report == 'bookings':
        writer.writerow(['Date', 'Bookings'])
        rows = Booking.objects.filter(booked_at__gte=start_at, booked_at__lt=end_exclusive).annotate(
            day=TruncDate('booked_at')
        ).values('day').annotate(bookings=Count('id')).order_by('day').iterator()
        for row in rows:
            writer.writerow([row['day'], row['bookings']])
    elif report == 'movies':
        writer.writerow(['Movie', 'Bookings'])
        rows = Booking.objects.filter(booked_at__gte=start_at, booked_at__lt=end_exclusive).values(
            'movie__name'
        ).annotate(bookings=Count('id')).order_by('-bookings', 'movie__name').iterator()
        for row in rows:
            writer.writerow([row['movie__name'], row['bookings']])
    elif report == 'theaters':
        writer.writerow(['Theater', 'Total seats', 'Booked seats', 'Occupancy percentage'])
        rows = Theater.objects.annotate(
            total_seats=Count('seats', distinct=True),
            booked_seats=Count('seats__booking', filter=Q(
                seats__booking__booked_at__gte=start_at, seats__booking__booked_at__lt=end_exclusive
            ), distinct=True),
        ).values('name', 'total_seats', 'booked_seats').order_by('-booked_seats', 'name').iterator()
        for row in rows:
            occupancy_percentage = round(row['booked_seats'] * 100 / row['total_seats'], 2) if row['total_seats'] else 0
            writer.writerow([row['name'], row['total_seats'], row['booked_seats'], occupancy_percentage])
    elif report == 'users':
        writer.writerow(['Date', 'New users'])
        rows = User.objects.filter(date_joined__gte=start_at, date_joined__lt=end_exclusive).annotate(
            day=TruncDate('date_joined')
        ).values('day').annotate(users=Count('id')).order_by('day').iterator()
        for row in rows:
            writer.writerow([row['day'], row['users']])
    else:
        return HttpResponseBadRequest('Unknown report')
    return response


@user_passes_test(staff_check, login_url='/login/')
def manage_movies(request):
    if not request.user.has_perm('movies.view_movie'):
        return HttpResponseForbidden('Permission denied.')
    movies = Movie.objects.all().order_by('-id')
    return render(request, 'movies/custom_admin/movies_list.html', {'movies': movies})


@user_passes_test(staff_check, login_url='/login/')
def add_edit_movie(request, movie_id=None):
    movie = get_object_or_404(Movie, id=movie_id) if movie_id else None
    required_permission = 'movies.change_movie' if movie else 'movies.add_movie'
    if not request.user.has_perm(required_permission):
        return HttpResponseForbidden('Permission denied.')
    genres = Genre.objects.all()
    languages = Language.objects.all()
    cast_list = CastMember.objects.all()

    if request.method == 'POST':
        name = request.POST.get('name')
        description = request.POST.get('description', '')
        duration = request.POST.get('duration', 120)
        age_certification = request.POST.get('age_certification', 'UA')
        trailer_youtube_url = request.POST.get('trailer_youtube_url', '')
        is_trending = request.POST.get('is_trending') == 'on'
        
        if movie is None:
            movie = Movie()

        movie.name = name
        movie.description = description
        movie.duration = int(duration) if duration else 120
        movie.age_certification = age_certification
        movie.trailer_youtube_url = trailer_youtube_url
        movie.is_trending = is_trending

        if 'image' in request.FILES:
            movie.image = request.FILES['image']

        movie.save()

        # Update M2M
        genre_ids = request.POST.getlist('genres')
        language_ids = request.POST.getlist('languages')
        cast_ids = request.POST.getlist('cast_members')

        movie.genres.set(genre_ids)
        movie.languages.set(language_ids)
        movie.cast_members.set(cast_ids)

        # Poster Uploads
        extra_posters = request.FILES.getlist('extra_posters')
        for poster_file in extra_posters:
            MoviePoster.objects.create(movie=movie, image=poster_file)

        messages.success(request, f"Movie '{movie.name}' saved successfully!")
        return redirect('manage_movies')

    context = {
        'movie': movie,
        'genres': genres,
        'languages': languages,
        'cast_list': cast_list,
    }
    return render(request, 'movies/custom_admin/movie_form.html', context)


@user_passes_test(staff_check, login_url='/login/')
def delete_movie(request, movie_id):
    if not request.user.has_perm('movies.delete_movie'):
        return HttpResponseForbidden('Permission denied.')
    movie = get_object_or_404(Movie, id=movie_id)
    if request.method == 'POST':
        movie.delete()
        messages.success(request, f"Movie '{movie.name}' deleted.")
        return redirect('manage_movies')
    return render(request, 'movies/custom_admin/confirm_delete.html', {'object': movie, 'type': 'Movie'})


@user_passes_test(staff_check, login_url='/login/')
def moderate_reviews(request):
    if not request.user.has_perm('movies.change_review'):
        return HttpResponseForbidden('Permission denied.')
    flagged_reviews = Review.objects.filter(is_flagged=True).select_related('user', 'movie').prefetch_related('reports')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        review_id = request.POST.get('review_id')
        review = get_object_or_404(Review, id=review_id)

        if action == 'dismiss':
            review.is_flagged = False
            review.save()
            review.reports.all().delete()
            messages.success(request, "Report dismissed. Review restored.")
        elif action == 'delete':
            review.delete()
            messages.success(request, "Inappropriate review deleted.")

        return redirect('moderate_reviews')

    return render(request, 'movies/custom_admin/moderation.html', {'flagged_reviews': flagged_reviews})


@user_passes_test(staff_check, login_url='/login/')
def manage_genres(request):
    if not request.user.has_perm('movies.view_genre'):
        return HttpResponseForbidden('Permission denied.')
    if request.method == 'POST':
        if not request.user.has_perm('movies.add_genre'):
            return HttpResponseForbidden('Permission denied.')
        name = request.POST.get('name', '').strip()
        if name:
            Genre.objects.get_or_create(name=name)
            messages.success(request, f"Genre '{name}' added.")
        return redirect('manage_genres')

    genres = Genre.objects.annotate(movie_count=Count('movies'))
    return render(request, 'movies/custom_admin/genres_list.html', {'genres': genres})


@user_passes_test(staff_check, login_url='/login/')
def delete_genre(request, genre_id):
    if not request.user.has_perm('movies.delete_genre'):
        return HttpResponseForbidden('Permission denied.')
    genre = get_object_or_404(Genre, id=genre_id)
    if request.method == 'POST':
        genre.delete()
        messages.success(request, f"Genre '{genre.name}' deleted.")
    return redirect('manage_genres')


@user_passes_test(staff_check, login_url='/login/')
def manage_cast(request):
    if not request.user.has_perm('movies.view_castmember'):
        return HttpResponseForbidden('Permission denied.')
    if request.method == 'POST':
        if not request.user.has_perm('movies.add_castmember'):
            return HttpResponseForbidden('Permission denied.')
        name = request.POST.get('name', '').strip()
        role = request.POST.get('role', 'Actor')
        bio = request.POST.get('bio', '')
        photo = request.FILES.get('photo')

        if name:
            CastMember.objects.create(name=name, role=role, bio=bio, photo=photo)
            messages.success(request, f"Cast member '{name}' added.")
        return redirect('manage_cast')

    cast_members = CastMember.objects.all()
    return render(request, 'movies/custom_admin/cast_list.html', {'cast_members': cast_members})


@user_passes_test(staff_check, login_url='/login/')
def delete_cast(request, cast_id):
    if not request.user.has_perm('movies.delete_castmember'):
        return HttpResponseForbidden('Permission denied.')
    cast = get_object_or_404(CastMember, id=cast_id)
    if request.method == 'POST':
        cast.delete()
        messages.success(request, f"Cast member '{cast.name}' deleted.")
    return redirect('manage_cast')
