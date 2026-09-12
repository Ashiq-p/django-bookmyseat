from pathlib import Path

from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command
from movies.models import Movie


class Command(BaseCommand):
    help = "Loads movie data from movies_data.json if movie data does not already exist."

    def handle(self, *args, **options):
        if Movie.objects.exists():
            self.stdout.write(
                self.style.WARNING(
                    "Movie data already exists. Skipping fixture load."
                )
            )
            return

        fixture_path = Path(settings.BASE_DIR) / "movies_data.json"

        if not fixture_path.is_file():
            raise CommandError(f"Fixture not found: {fixture_path}")

        self.stdout.write(f"Loading fixture: {fixture_path}")

        call_command(
            "loaddata",
            str(fixture_path),
            verbosity=options.get("verbosity", 1),
        )

        self.stdout.write(
            self.style.SUCCESS("Movie data loaded successfully.")
        )