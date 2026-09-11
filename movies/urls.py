from django.urls import path
from . import views

urlpatterns = [
    path('', views.movie_list, name='movie_list'),
    path('count/', views.movie_count, name='movie_count'),
    path('<int:movie_id>/', views.movie_detail, name='movie_detail'),
    path('<int:movie_id>/theaters/', views.theater_list, name='theater_list'),
    path('theater/<int:theater_id>/seats/', views.book_seat, name='book_seat'),
    path('theater/<int:theater_id>/payment/', views.start_payment, name='start_payment'),
    path('payment/<int:payment_id>/retry/', views.retry_payment, name='retry_payment'),
    path('payment/<int:payment_id>/cancel/', views.cancel_payment, name='cancel_payment'),
    path('payment/callback/', views.payment_callback, name='payment_callback'),
    path('payment/webhook/razorpay/', views.razorpay_webhook, name='razorpay_webhook'),
    path('ticket/<int:booking_id>/download/', views.download_ticket, name='download_ticket'),
    path('ticket/verify/<str:token>/', views.verify_ticket, name='verify_ticket'),
    path('<int:movie_id>/review/', views.add_or_edit_review, name='add_or_edit_review'),
    path('review/<int:review_id>/report/', views.report_review, name='report_review'),

    # Custom Admin Routes
    path('custom-admin/', views.custom_admin_dashboard, name='custom_admin_dashboard'),
    path('custom-admin/analytics/export/', views.export_analytics_csv, name='export_analytics_csv'),
    path('custom-admin/movies/', views.manage_movies, name='manage_movies'),
    path('custom-admin/movies/add/', views.add_edit_movie, name='add_movie'),
    path('custom-admin/movies/<int:movie_id>/edit/', views.add_edit_movie, name='edit_movie'),
    path('custom-admin/movies/<int:movie_id>/delete/', views.delete_movie, name='delete_movie'),
    path('custom-admin/moderation/', views.moderate_reviews, name='moderate_reviews'),
    path('custom-admin/genres/', views.manage_genres, name='manage_genres'),
    path('custom-admin/genres/<int:genre_id>/delete/', views.delete_genre, name='delete_genre'),
    path('custom-admin/cast/', views.manage_cast, name='manage_cast'),
    path('custom-admin/cast/<int:cast_id>/delete/', views.delete_cast, name='delete_cast'),
]
