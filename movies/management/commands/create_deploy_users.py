import os

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User


class Command(BaseCommand):
    help = "Create required deployment users if they do not exist"

    def handle(self, *args, **options):

        users = [
            {
                "username": "ashiq",
                "email": "cpyashique@gmail.com",
                "password": os.environ.get("DEPLOY_ASHIQ_PASSWORD"),
                "is_staff": True,
                "is_superuser": True,
            },
            {
                "username": "sample",
                "email": "sample@gmail.com",
                "password": os.environ.get("DEPLOY_SAMPLE_PASSWORD"),
                "is_staff": False,
                "is_superuser": False,
            },
            {
                "username": "demouser",
                "email": "demo@example.com",
                "password": os.environ.get("DEPLOY_DEMOUSER_PASSWORD"),
                "is_staff": False,
                "is_superuser": False,
            },
        ]

        for data in users:
            username = data["username"]
            password = data["password"]

            if not password:
                self.stdout.write(
                    self.style.ERROR(
                        f"Password missing for {username}"
                    )
                )
                continue

            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": data["email"],
                    "is_staff": data["is_staff"],
                    "is_superuser": data["is_superuser"],
                },
            )

            if created:
                user.set_password(password)
                user.save()

                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created user: {username}"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"User already exists: {username}"
                    )
                )