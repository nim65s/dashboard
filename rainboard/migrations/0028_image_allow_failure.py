# Generated by Django 2.1.5 on 2019-02-08 16:25

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rainboard', '0027_project_suffix'),
    ]

    operations = [
        migrations.AddField(
            model_name='image',
            name='allow_failure',
            field=models.BooleanField(default=False),
        ),
    ]