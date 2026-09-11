from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('movies', '0009_booking_ticket_pdf')]

    operations = [
        migrations.AddField(
            model_name='theater',
            name='screen_name',
            field=models.CharField(blank=True, default='Screen 1', max_length=100),
        ),
    ]