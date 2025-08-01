# Generated by Django 3.2.12 on 2022-03-16 16:47

from django.db import migrations, models
import django_lifecycle.mixins
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_squashed_0090_char_to_text_field'),
    ]

    operations = [
        migrations.CreateModel(
            name='SystemID',
            fields=[
                ('pulp_id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ],
            options={
                'ordering': ('pulp_id',),
            },
            bases=(django_lifecycle.mixins.LifecycleModelMixin, models.Model),
        ),
    ]
