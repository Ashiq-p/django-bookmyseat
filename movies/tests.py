from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch
import hashlib
import hmac
import json
from django.conf import settings
from django.core import mail
from django.test import override_settings
from django.contrib.auth.models import Permission

from .models import Booking, Movie, MovieView, Payment, PaymentWebhookEvent, Seat, SeatReservation, Theater
from .tasks import generate_and_email_ticket


class SeatReservationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='test-password')
        self.other_user = User.objects.create_user(username='bob', password='test-password')
        movie = Movie.objects.create(name='Reservation test')
        self.theater = Theater.objects.create(name='Test theater', movie=movie, time=timezone.now())
        self.seat = Seat.objects.create(theater=self.theater, seat_number='A1')
        self.url = reverse('book_seat', args=[self.theater.id])

    @override_settings(RAZORPAY_KEY_ID='key_test', RAZORPAY_KEY_SECRET='secret_test')
    @patch('movies.views.razorpay_api', return_value={'id': 'order_test_1', 'amount': 20000, 'currency': 'INR'})
    def test_proceed_to_payment_only_creates_pending_payment(self, razorpay_api):
        self.client.login(username='alice', password='test-password')
        self.client.post(self.url, {'action': 'reserve', 'seats': [self.seat.id]})
        reservation = SeatReservation.objects.get(seat=self.seat)
        self.assertEqual(reservation.user, self.user)
        self.assertGreater(reservation.expires_at, timezone.now())

        response = self.client.post(reverse('start_payment', args=[self.theater.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pay securely with Razorpay')
        self.assertTrue(SeatReservation.objects.filter(seat=self.seat).exists())
        self.assertFalse(Booking.objects.filter(seat=self.seat).exists())
        self.assertEqual(Payment.objects.get().status, 'pending')

    @override_settings(RAZORPAY_KEY_ID='key_test', RAZORPAY_KEY_SECRET='secret_test')
    @patch('movies.views.razorpay_api', return_value={'status': 'captured', 'order_id': 'order_test_2', 'amount': 20000, 'currency': 'INR'})
    def test_verified_callback_confirms_once_and_duplicate_is_idempotent(self, razorpay_api):
        self.client.login(username='alice', password='test-password')
        SeatReservation.objects.create(
            user=self.user, seat=self.seat, theater=self.theater,
            expires_at=timezone.now() + timedelta(minutes=2),
        )
        with patch('movies.views.razorpay_api', return_value={'id': 'order_test_2', 'amount': 20000, 'currency': 'INR'}):
            self.client.post(reverse('start_payment', args=[self.theater.id]))
        payment = Payment.objects.get()
        payment_id = 'pay_test_2'
        signature = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode(),
            f'{payment.razorpay_order_id}|{payment_id}'.encode(),
            hashlib.sha256,
        ).hexdigest()
        payload = {'razorpay_order_id': payment.razorpay_order_id, 'razorpay_payment_id': payment_id, 'razorpay_signature': signature}
        self.client.post(reverse('payment_callback'), payload)
        self.client.post(reverse('payment_callback'), payload)
        self.assertEqual(Booking.objects.filter(seat=self.seat).count(), 1)
        self.assertEqual(Payment.objects.get().status, 'paid')

    def test_authenticated_movie_views_are_unique_and_anonymous_views_are_not_saved(self):
        movie = self.theater.movie
        self.assertEqual(self.client.get(reverse('movie_detail', args=[movie.id])).status_code, 200)
        self.assertEqual(MovieView.objects.count(), 0)

        self.client.login(username='alice', password='test-password')
        self.assertEqual(self.client.get(reverse('movie_detail', args=[movie.id])).status_code, 200)
        first_view = MovieView.objects.get(user=self.user, movie=movie)
        first_timestamp = first_view.viewed_at
        self.assertEqual(self.client.get(reverse('movie_detail', args=[movie.id])).status_code, 200)
        second_view = MovieView.objects.get(user=self.user, movie=movie)

        self.assertEqual(MovieView.objects.filter(user=self.user, movie=movie).count(), 1)
        self.assertGreaterEqual(second_view.viewed_at, first_timestamp)

    def test_callback_rejects_gateway_amount_and_currency_mismatches(self):
        self.client.login(username='alice', password='test-password')
        self.client.post(self.url, {'action': 'reserve', 'seats': [self.seat.id]})

        with patch('movies.views.razorpay_api', return_value={'id': 'order_test_mismatch', 'amount': 20000, 'currency': 'INR'}):
            self.client.post(reverse('start_payment', args=[self.theater.id]))
        payment = Payment.objects.get()
        payment_id = 'pay_test_mismatch'
        signature = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode(),
            f'{payment.razorpay_order_id}|{payment_id}'.encode(),
            hashlib.sha256,
        ).hexdigest()

        with patch('movies.views.razorpay_api', return_value={
            'status': 'captured',
            'order_id': payment.razorpay_order_id,
            'amount': payment.amount + 1,
            'currency': 'USD',
        }):
            self.client.post(reverse('payment_callback'), {
                'razorpay_order_id': payment.razorpay_order_id,
                'razorpay_payment_id': payment_id,
                'razorpay_signature': signature,
            })

        payment.refresh_from_db()
        self.assertEqual(payment.status, 'failed')
        self.assertFalse(Booking.objects.filter(seat=self.seat).exists())

    @override_settings(RAZORPAY_KEY_ID='key_test', RAZORPAY_KEY_SECRET='secret_test', RAZORPAY_WEBHOOK_SECRET='webhook_test')
    @patch('movies.views.razorpay_api', return_value={'id': 'order_webhook_test', 'amount': 20000, 'currency': 'INR'})
    def test_duplicate_webhook_does_not_duplicate_booking(self, razorpay_api):
        self.client.login(username='alice', password='test-password')
        self.client.post(self.url, {'action': 'reserve', 'seats': [self.seat.id]})
        self.client.post(reverse('start_payment', args=[self.theater.id]))
        payment = Payment.objects.get()
        payload = {
            'event': 'payment.captured',
            'payload': {'payment': {'entity': {
                'id': 'pay_webhook_test',
                'order_id': payment.razorpay_order_id,
                'amount': payment.amount,
                'currency': payment.currency,
            }}},
        }
        raw_body = json.dumps(payload).encode()
        signature = hmac.new(b'webhook_test', raw_body, hashlib.sha256).hexdigest()
        headers = {
            'HTTP_X_RAZORPAY_SIGNATURE': signature,
            'HTTP_X_RAZORPAY_EVENT_ID': 'event_webhook_test',
        }

        first = self.client.post(reverse('razorpay_webhook'), raw_body, content_type='application/json', **headers)
        second = self.client.post(reverse('razorpay_webhook'), raw_body, content_type='application/json', **headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Booking.objects.filter(seat=self.seat).count(), 1)
        self.assertEqual(PaymentWebhookEvent.objects.filter(event_id='event_webhook_test').count(), 1)

    def test_active_reservation_blocks_another_user(self):
        SeatReservation.objects.create(
            user=self.user, seat=self.seat, theater=self.theater,
            expires_at=timezone.now() + timedelta(minutes=2),
        )
        self.client.login(username='bob', password='test-password')
        self.client.post(self.url, {'action': 'reserve', 'seats': [self.seat.id]})
        self.assertEqual(SeatReservation.objects.get(seat=self.seat).user, self.user)

    def test_expired_reservation_is_released(self):
        SeatReservation.objects.create(
            user=self.user, seat=self.seat, theater=self.theater,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.client.login(username='bob', password='test-password')
        self.client.post(self.url, {'action': 'reserve', 'seats': [self.seat.id]})
        self.assertEqual(SeatReservation.objects.get(seat=self.seat).user, self.other_user)

    def test_analytics_requires_permission_and_exports_aggregates(self):
        admin = User.objects.create_user(username='analytics-admin', password='test-password', is_staff=True)
        admin.user_permissions.add(Permission.objects.get(codename='view_payment'))
        payment = Payment.objects.create(
            user=self.user,
            theater=self.theater,
            seat_ids=[self.seat.id],
            amount=20000,
            status='paid',
            razorpay_order_id='order_analytics_1',
            razorpay_payment_id='pay_analytics_1',
        )
        Booking.objects.create(user=self.user, seat=self.seat, movie=self.theater.movie, theater=self.theater, payment=payment)

        self.client.login(username='bob', password='test-password')
        self.assertEqual(self.client.get(reverse('custom_admin_dashboard')).status_code, 302)

        self.client.login(username='analytics-admin', password='test-password')
        response = self.client.get(reverse('custom_admin_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Theater occupancy')
        export = self.client.get(reverse('export_analytics_csv'), {'report': 'movies'})
        self.assertEqual(export.status_code, 200)
        self.assertIn('Reservation test', export.content.decode())


class TicketGenerationTests(TestCase):
    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_paid_booking_generates_and_emails_pdf_ticket(self):
        user = User.objects.create_user(
            username='ticket-user', password='test-password', email='ticket@example.com'
        )
        movie = Movie.objects.create(name='Ticket Movie')
        theater = Theater.objects.create(
            name='Ticket Theater', location='Downtown', screen_name='Screen 2',
            movie=movie, time=timezone.now() + timedelta(days=1),
        )
        seat = Seat.objects.create(theater=theater, seat_number='B4')
        payment = Payment.objects.create(
            user=user, theater=theater, seat_ids=[seat.id], amount=20000,
            status='paid', razorpay_order_id='order_ticket_1',
            razorpay_payment_id='pay_ticket_1',
        )
        booking = Booking.objects.create(
            user=user, seat=seat, movie=movie, theater=theater, payment=payment,
        )

        generate_and_email_ticket.run(payment.id)

        booking.refresh_from_db()
        self.assertTrue(booking.ticket_pdf.name)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['ticket@example.com'])
        attachment = mail.outbox[0].attachments[0]
        self.assertEqual(attachment[0], f'bookmyseat-ticket-{booking.id}.pdf')
        self.assertEqual(attachment[2], 'application/pdf')
        self.assertTrue(attachment[1].startswith(b'%PDF'))
