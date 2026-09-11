from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from movies.models import (
    Genre, Language, CastMember, Movie, MoviePoster,
    Theater, Seat, Booking, Review
)

class Command(BaseCommand):
    help = "Seed database with sample genres, languages, cast, movies, theaters, showtimes, and demo reviews."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting database seeding..."))

        # 1. Seed Genres
        genre_names = ['Action', 'Sci-Fi', 'Drama', 'Comedy', 'Thriller', 'Romance']
        genres = {}
        for name in genre_names:
            g, _ = Genre.objects.get_or_create(name=name)
            genres[name] = g
        self.stdout.write(f"Created/found {len(genres)} genres.")

        # 2. Seed Languages
        langs_data = [('English', 'EN'), ('Hindi', 'HI'), ('Spanish', 'ES')]
        languages = {}
        for name, code in langs_data:
            l, _ = Language.objects.get_or_create(name=name, defaults={'code': code})
            languages[name] = l
        self.stdout.write(f"Created/found {len(languages)} languages.")

        # 3. Seed Cast Members
        cast_data = [
            ("Christopher Nolan", "Director", "Acclaimed visionary filmmaker."),
            ("Leonardo DiCaprio", "Actor", "Oscar-winning actor."),
            ("Cillian Murphy", "Actor", "Renowned stage and screen actor."),
            ("Margot Robbie", "Actress", "Multi-talented actress and producer."),
            ("Christian Bale", "Actor", "Master of transformative roles.")
        ]
        cast_members = {}
        for name, role, bio in cast_data:
            c, _ = CastMember.objects.get_or_create(name=name, defaults={'role': role, 'bio': bio})
            cast_members[name] = c
        self.stdout.write(f"Created/found {len(cast_members)} cast members.")

        # 4. Seed Movies
        movies_data = [
            {
                'name': 'Inception',
                'description': 'A thief who steals corporate secrets through the use of dream-sharing technology is given the inverse task of planting an idea into the mind of a C.E.O.',
                'duration': 148,
                'age_certification': 'UA',
                'trailer_youtube_url': 'https://www.youtube.com/watch?v=YoHD9XEInc0',
                'release_date': timezone.now().date() - timedelta(days=120),
                'is_trending': True,
                'genres': ['Action', 'Sci-Fi', 'Thriller'],
                'languages': ['English'],
                'cast': ['Christopher Nolan', 'Leonardo DiCaprio'],
            },
            {
                'name': 'Oppenheimer',
                'description': 'The story of American scientist J. Robert Oppenheimer and his role in the development of the atomic bomb during WWII.',
                'duration': 180,
                'age_certification': 'A',
                'trailer_youtube_url': 'https://www.youtube.com/watch?v=uYPbbksJxIg',
                'release_date': timezone.now().date() - timedelta(days=60),
                'is_trending': True,
                'genres': ['Drama', 'Thriller'],
                'languages': ['English'],
                'cast': ['Christopher Nolan', 'Cillian Murphy'],
            },
            {
                'name': 'The Dark Knight',
                'description': 'When the menace known as the Joker wreaks havoc and chaos on the people of Gotham, Batman must accept one of the greatest psychological tests of his ability to fight injustice.',
                'duration': 152,
                'age_certification': 'UA',
                'trailer_youtube_url': 'https://www.youtube.com/watch?v=EXeTwQWrcwY',
                'release_date': timezone.now().date() - timedelta(days=365),
                'is_trending': False,
                'genres': ['Action', 'Drama', 'Thriller'],
                'languages': ['English'],
                'cast': ['Christopher Nolan', 'Christian Bale'],
            },
            {
                'name': 'Barbie',
                'description': 'Barbie and Ken are having the time of their lives in the colorful and seemingly perfect world of Barbie Land.',
                'duration': 114,
                'age_certification': 'PG-13',
                'trailer_youtube_url': 'https://www.youtube.com/watch?v=pBk4NYhWNMM',
                'release_date': timezone.now().date() - timedelta(days=90),
                'is_trending': True,
                'genres': ['Comedy', 'Romance'],
                'languages': ['English'],
                'cast': ['Margot Robbie'],
            },
        ]

        created_movies = []
        for mdata in movies_data:
            movie, created = Movie.objects.get_or_create(
                name=mdata['name'],
                defaults={
                    'description': mdata['description'],
                    'duration': mdata['duration'],
                    'age_certification': mdata['age_certification'],
                    'trailer_youtube_url': mdata['trailer_youtube_url'],
                    'release_date': mdata['release_date'],
                    'is_trending': mdata['is_trending'],
                }
            )
            # Add relationships
            for gname in mdata['genres']:
                if gname in genres:
                    movie.genres.add(genres[gname])
            for lname in mdata['languages']:
                if lname in languages:
                    movie.languages.add(languages[lname])
            for cname in mdata['cast']:
                if cname in cast_members:
                    movie.cast_members.add(cast_members[cname])

            created_movies.append(movie)

        self.stdout.write(f"Created/found {len(created_movies)} movies.")

        # 5. Create Theaters & Past / Future Showtimes
        theaters_data = [
            ("Grand IMAX Screen 1", "Downtown Center"),
            ("PVR Superplex Hall 2", "Westside Mall"),
            ("Cinepolis VIP Suite", "Central Square")
        ]

        now = timezone.now()

        for movie in created_movies:
            # Past showtime (yesterday) for testing reviews
            past_theater, _ = Theater.objects.get_or_create(
                name=f"{theaters_data[0][0]}",
                movie=movie,
                time=now - timedelta(days=1),
                defaults={'location': theaters_data[0][1]}
            )
            # Future showtime (tomorrow) for booking new tickets
            future_theater, _ = Theater.objects.get_or_create(
                name=f"{theaters_data[1][0]}",
                movie=movie,
                time=now + timedelta(days=1),
                defaults={'location': theaters_data[1][1]}
            )

            # Generate seats for both theaters
            for th in [past_theater, future_theater]:
                for row in ['A', 'B', 'C']:
                    for num in range(1, 6):
                        seat_num = f"{row}{num}"
                        Seat.objects.get_or_create(theater=th, seat_number=seat_num)

        self.stdout.write("Created sample theaters, past/future showtimes, and seats.")

        # 6. Create Demo Users & Bookings/Reviews
        demo_user, _ = User.objects.get_or_create(
            username='demouser',
            defaults={'email': 'demo@example.com', 'is_staff': False}
        )
        if not demo_user.check_password('password123'):
            demo_user.set_password('password123')
            demo_user.save()

        # Create past booking for demouser on Inception
        inception = Movie.objects.get(name='Inception')
        past_th = Theater.objects.filter(movie=inception, time__lt=now).first()
        if past_th:
            seat = past_th.seats.first()
            booking, _ = Booking.objects.get_or_create(
                user=demo_user,
                seat=seat,
                movie=inception,
                theater=past_th
            )
            seat.is_booked = True
            seat.save()

            # Create sample review
            Review.objects.get_or_create(
                user=demo_user,
                movie=inception,
                defaults={
                    'rating': 5,
                    'comment': "Mind-blowing masterpiece! The visuals and score are unmatched.",
                    'is_verified_viewer': True
                }
            )

        self.stdout.write(self.style.SUCCESS("Database seeding completed successfully!"))
