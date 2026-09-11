from io import BytesIO
import logging

import qrcode
from celery import shared_task
from django.conf import settings
from django.core import signing
from django.core.files.base import ContentFile
from django.core.mail import EmailMessage
from django.urls import reverse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as PdfImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from django.utils import timezone

from .models import Booking

logger = logging.getLogger(__name__)


def ticket_token(booking_id):
    return signing.dumps({'booking_id': booking_id}, salt='ticket-verification')


def ticket_url(booking):
    path = reverse('verify_ticket', args=[ticket_token(booking.id)])
    return f'{settings.TICKET_VERIFY_BASE_URL.rstrip("/")}{path}' if settings.TICKET_VERIFY_BASE_URL else path


def poster_image(movie):
    """Return the first usable movie poster as a reportlab flowable."""
    image_field = movie.image
    if not image_field:
        poster = movie.posters.order_by('created_at').first()
        image_field = poster.image if poster else None
    if not image_field:
        return None
    try:
        with image_field.open('rb') as image_file:
            image_data = image_file.read()
        return PdfImage(BytesIO(image_data), width=35 * mm, height=50 * mm, kind='proportional')
    except (OSError, ValueError, TypeError):
        logger.warning('Could not add poster to ticket for movie %s.', movie.pk, exc_info=True)
        return None


def build_ticket_pdf(booking):
    payment = booking.payment
    seats = Booking.objects.filter(payment=payment).select_related('seat').order_by('seat__seat_number')
    qr_buffer = BytesIO()
    qrcode.make(ticket_url(booking)).save(qr_buffer, format='PNG')
    qr_buffer.seek(0)

    buffer = BytesIO()
    styles = getSampleStyleSheet()
    story = [Paragraph('BOOKMYSEAT', styles['Title']), Paragraph('Electronic Movie Ticket', styles['Heading2'])]
    poster = poster_image(booking.movie)
    if poster:
        story.extend([poster, Spacer(1, 8)])
    else:
        story.append(Spacer(1, 8))
    booked_at = timezone.localtime(booking.booked_at).strftime('%d %B %Y, %I:%M %p')
    rows = [
        ['Movie', booking.movie.name],
        ['Theater', booking.theater.name],
        ['Location', booking.theater.location],
        ['Screen', booking.theater.screen_name],
        ['Show', booking.theater.time.strftime('%A, %d %B %Y at %I:%M %p')],
        ['Seats', ', '.join(item.seat.seat_number for item in seats)],
        ['Booking ID', str(booking.id)],
        ['Payment ID', payment.razorpay_payment_id or payment.razorpay_order_id],
        ['Amount Paid', f'{payment.currency} {payment.amount_rupees:,.2f}'],
        ['Booking Date', booked_at],
        ['Customer', booking.user.get_full_name() or booking.user.username],
    ]
    table = Table(rows, colWidths=[32 * mm, 125 * mm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#18324b')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#ccd5dd')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('PADDING', (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 14), Paragraph('Scan to verify this ticket', styles['Heading3']), PdfImage(qr_buffer, width=35 * mm, height=35 * mm)])
    SimpleDocTemplate(buffer, pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm).build(story)
    return buffer.getvalue()


@shared_task(bind=True, autoretry_for=(), max_retries=None)
def generate_and_email_ticket(self, payment_id):
    bookings = list(Booking.objects.filter(payment_id=payment_id).select_related('user', 'movie', 'theater', 'seat', 'payment'))
    if not bookings:
        return
    booking = bookings[0]
    if not booking.ticket_pdf:
        pdf = build_ticket_pdf(booking)
        filename = f'bookmyseat-ticket-{booking.id}.pdf'
        for item in bookings:
            item.ticket_pdf.save(filename, ContentFile(pdf), save=True)
    else:
        booking.ticket_pdf.open('rb')
        pdf = booking.ticket_pdf.read()
        booking.ticket_pdf.close()

    payment = booking.payment
    if not booking.user.email:
        logger.warning('Booking %s cannot receive a ticket because the user has no email address.', booking.id)
        return
    body = (
        f'Your booking is confirmed.\n\nMovie: {booking.movie.name}\n'
        f'Theater: {booking.theater.name}\nLocation: {booking.theater.location}\n'
        f'Screen: {booking.theater.screen_name}\nShow: {booking.theater.time:%d %B %Y, %I:%M %p}\n'
        f'Seats: {", ".join(item.seat.seat_number for item in bookings)}\n'
        f'Booking ID: {booking.id}\nPayment ID: {payment.razorpay_payment_id or payment.razorpay_order_id}\n'
        f'Amount paid: {payment.currency} {payment.amount_rupees:,.2f}\n'
        f'Booked at: {timezone.localtime(booking.booked_at):%d %B %Y, %I:%M %p}'
    )
    email = EmailMessage(f'Booking confirmed: {booking.movie.name}', body, settings.DEFAULT_FROM_EMAIL, [booking.user.email])
    email.attach(f'bookmyseat-ticket-{booking.id}.pdf', pdf, 'application/pdf')
    try:
        email.send(fail_silently=False)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=settings.TICKET_EMAIL_RETRY_DELAY, max_retries=settings.TICKET_EMAIL_MAX_RETRIES)