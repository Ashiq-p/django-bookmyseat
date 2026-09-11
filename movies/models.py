from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.text import slugify
import re

class Genre(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Language(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=10, unique=True, blank=True, null=True)

    def __str__(self):
        return self.name


class CastMember(models.Model):
    ROLE_CHOICES = [
        ('Actor', 'Actor'),
        ('Actress', 'Actress'),
        ('Director', 'Director'),
        ('Producer', 'Producer'),
        ('Writer', 'Writer'),
    ]
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=50, choices=ROLE_CHOICES, default='Actor')
    photo = models.ImageField(upload_to='cast/', blank=True, null=True)
    bio = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.name} ({self.role})"


class Movie(models.Model):
    AGE_CERTIFICATION_CHOICES = [
        ('U', 'U (Universal)'),
        ('UA', 'UA (Parental Guidance)'),
        ('A', 'A (Adults Only)'),
        ('S', 'S (Specialized)'),
        ('PG-13', 'PG-13'),
        ('R', 'R (Restricted)'),
    ]

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    image = models.ImageField(upload_to='movies/', blank=True, null=True)
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=0.0)
    cast = models.TextField(blank=True, help_text="Legacy cast names comma separated")
    description = models.TextField(blank=True, null=True)
    duration = models.PositiveIntegerField(default=120, help_text="Duration in minutes")
    age_certification = models.CharField(max_length=10, choices=AGE_CERTIFICATION_CHOICES, default='UA')
    trailer_youtube_url = models.CharField(max_length=500, blank=True, null=True, help_text="YouTube Watch or Embed URL")
    release_date = models.DateField(default=timezone.now)
    is_trending = models.BooleanField(default=False)
    
    genres = models.ManyToManyField(Genre, related_name='movies', blank=True)
    languages = models.ManyToManyField(Language, related_name='movies', blank=True)
    cast_members = models.ManyToManyField(CastMember, related_name='movies', blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['release_date']),
            models.Index(fields=['rating']),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            self.slug = base_slug
        super().save(*args, **kwargs)

    @property
    def youtube_embed_url(self):
        if not self.trailer_youtube_url:
            return None
        url = self.trailer_youtube_url.strip()
        match = re.search(r'(?:v=|\/embed\/|\/1\.1\/|youtu\.be\/|\/v\/|e\/|watch\?v=|\?v=)([^#\&\?]*);?', url)
        if match and len(match.group(1)) == 11:
            video_id = match.group(1)
            return f"https://www.youtube-nocookie.com/embed/{video_id}"
        elif len(url) == 11 and re.match(r'^[A-Za-z0-9_-]{11}$', url):
            return f"https://www.youtube-nocookie.com/embed/{url}"
        return url

    @property
    def duration_formatted(self):
        if not self.duration:
            return "N/A"
        hours = self.duration // 60
        minutes = self.duration % 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def update_average_rating(self):
        avg_rating = self.reviews.aggregate(models.Avg('rating'))['rating__avg']
        if avg_rating is not None:
            self.rating = round(avg_rating, 1)
        else:
            self.rating = 0.0
        self.save(update_fields=['rating'])

    def __str__(self):
        return self.name


class MovieView(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'movie'],
                name='unique_user_movie_view',
            ),
        ]
        indexes = [
            models.Index(fields=['user', '-viewed_at']),
            models.Index(fields=['movie', '-viewed_at']),
        ]


class MoviePoster(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='posters')
    image = models.ImageField(upload_to='movies/posters/')
    caption = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Poster for {self.movie.name}"


class Theater(models.Model):
    name = models.CharField(max_length=255)
    location = models.CharField(max_length=255, blank=True, default='Main Screen')
    screen_name = models.CharField(max_length=100, blank=True, default='Screen 1')
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='theaters')
    time = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=['location', 'time']),
            models.Index(fields=['movie', 'time']),
        ]

    def __str__(self):
        return f'{self.name} - {self.movie.name} at {self.time.strftime("%Y-%m-%d %H:%M")}'


class Seat(models.Model):
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='seats')
    seat_number = models.CharField(max_length=10)
    is_booked = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.seat_number} in {self.theater.name}'


class Booking(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    seat = models.OneToOneField(Seat, on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE)
    payment = models.ForeignKey('Payment', on_delete=models.PROTECT, null=True, blank=True, related_name='bookings')
    ticket_pdf = models.FileField(upload_to='tickets/', blank=True, null=True)
    booked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['booked_at']),
            models.Index(fields=['theater', 'booked_at']),
            models.Index(fields=['movie', 'booked_at']),
        ]

    def __str__(self):
        return f'Booking by {self.user.username} for {self.seat.seat_number} at {self.theater.name}'


class SeatReservation(models.Model):
    """A short-lived seat hold while a customer completes payment."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='seat_reservations')
    seat = models.OneToOneField(Seat, on_delete=models.CASCADE, related_name='reservation')
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='reservations')
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['theater', 'expires_at'])]

    def __str__(self):
        return f'Reservation for {self.seat.seat_number} until {self.expires_at:%Y-%m-%d %H:%M}'


class Payment(models.Model):
    STATUS_CHOICES = [
        ('created', 'Created'), ('pending', 'Pending'), ('paid', 'Paid'),
        ('failed', 'Failed'), ('cancelled', 'Cancelled'), ('refunded', 'Refunded'),
    ]
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='payments')
    theater = models.ForeignKey(Theater, on_delete=models.PROTECT, related_name='payments')
    seat_ids = models.JSONField()
    amount = models.PositiveIntegerField(help_text='Amount in paise')
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='created')
    razorpay_order_id = models.CharField(max_length=100, unique=True)
    razorpay_payment_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    failure_reason = models.CharField(max_length=500, blank=True)
    refund_amount = models.PositiveIntegerField(default=0, help_text='Refunded amount in paise')
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['theater', 'status', 'created_at']),
        ]

    @property
    def amount_rupees(self):
        return self.amount / 100

    def __str__(self):
        return f'{self.razorpay_order_id} ({self.status})'


class PaymentWebhookEvent(models.Model):
    event_id = models.CharField(max_length=100, unique=True)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='webhook_events')
    event_type = models.CharField(max_length=100)
    received_at = models.DateTimeField(auto_now_add=True)


class Review(models.Model):
    RATING_CHOICES = [(i, f'{i} Star{"s" if i > 1 else ""}') for i in range(1, 6)]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='movie_reviews')
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(choices=RATING_CHOICES, default=5)
    comment = models.TextField()
    is_verified_viewer = models.BooleanField(default=False)
    is_flagged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'movie')
        ordering = ['-created_at']

    def __str__(self):
        return f"Review by {self.user.username} for {self.movie.name} ({self.rating} stars)"


class ReviewReport(models.Model):
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='review_reports')
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Report on review {self.review.id} by {self.reporter.username}"
