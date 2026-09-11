from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Booking, Seat, Review


@receiver(post_delete, sender=Booking)
def make_seat_available_when_booking_is_deleted(sender, instance, **kwargs):
    """Keep the seat availability flag in sync after an admin deletion."""
    Seat.objects.filter(pk=instance.seat_id).update(is_booked=False)


@receiver(post_save, sender=Review)
@receiver(post_delete, sender=Review)
def update_movie_rating_on_review_change(sender, instance, **kwargs):
    """Automatically calculate average rating when a review is created, edited, or deleted."""
    if instance.movie:
        instance.movie.update_average_rating()

