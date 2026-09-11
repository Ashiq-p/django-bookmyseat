from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('movies', '0004_payment_paymentwebhookevent'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='payment',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='bookings',
                to='movies.payment',
            ),
        ),
    ]