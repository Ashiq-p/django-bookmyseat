from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('movies', '0008_movie_movies_movi_release_b7ac7d_idx_and_more')]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='ticket_pdf',
            field=models.FileField(blank=True, null=True, upload_to='tickets/'),
        ),
    ]