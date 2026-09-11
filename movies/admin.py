from django.contrib import admin
from .models import (
    Genre, Language, CastMember, Movie, MoviePoster, MovieView,
    Theater, Seat, Booking, SeatReservation, Payment, PaymentWebhookEvent, Review, ReviewReport
)


class MoviePosterInline(admin.TabularInline):
    model = MoviePoster
    extra = 1


class TheaterInline(admin.TabularInline):
    model = Theater
    extra = 1


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ('name', 'code')


@admin.register(CastMember)
class CastMemberAdmin(admin.ModelAdmin):
    list_display = ('name', 'role')
    list_filter = ('role',)
    search_fields = ('name', 'bio')


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = ('name', 'age_certification', 'duration', 'rating', 'release_date', 'is_trending')
    list_filter = ('age_certification', 'is_trending', 'genres', 'languages')
    search_fields = ('name', 'description')
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ('genres', 'languages', 'cast_members')
    inlines = [MoviePosterInline, TheaterInline]


@admin.register(MoviePoster)
class MoviePosterAdmin(admin.ModelAdmin):
    list_display = ('movie', 'caption', 'created_at')


@admin.register(MovieView)
class MovieViewAdmin(admin.ModelAdmin):
    list_display = ('user', 'movie', 'viewed_at')
    list_filter = ('viewed_at',)
    search_fields = ('user__username', 'movie__name')


@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ('name', 'location', 'movie', 'time')
    list_filter = ('movie', 'time')
    search_fields = ('name', 'location', 'movie__name')


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ('seat_number', 'theater', 'is_booked')
    list_filter = ('is_booked', 'theater')
    search_fields = ('seat_number', 'theater__name')


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('user', 'movie', 'theater', 'seat', 'booked_at')
    list_filter = ('booked_at', 'movie')
    search_fields = ('user__username', 'movie__name', 'seat__seat_number')


@admin.register(SeatReservation)
class SeatReservationAdmin(admin.ModelAdmin):
    list_display = ('user', 'seat', 'theater', 'expires_at', 'created_at')
    list_filter = ('theater', 'expires_at')
    search_fields = ('user__username', 'seat__seat_number')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('razorpay_order_id', 'user', 'amount', 'currency', 'status', 'razorpay_payment_id', 'created_at')
    list_filter = ('status', 'currency')
    search_fields = ('razorpay_order_id', 'razorpay_payment_id', 'user__username')


@admin.register(PaymentWebhookEvent)
class PaymentWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('event_id', 'payment', 'event_type', 'received_at')


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('user', 'movie', 'rating', 'is_verified_viewer', 'is_flagged', 'created_at')
    list_filter = ('rating', 'is_verified_viewer', 'is_flagged', 'created_at')
    search_fields = ('user__username', 'movie__name', 'comment')
    actions = ['clear_flag']

    def clear_flag(self, request, queryset):
        queryset.update(is_flagged=False)
    clear_flag.short_description = "Clear reported flag from selected reviews"


@admin.register(ReviewReport)
class ReviewReportAdmin(admin.ModelAdmin):
    list_display = ('review', 'reporter', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('reporter__username', 'reason', 'review__comment')

