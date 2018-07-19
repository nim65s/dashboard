# Generated by Django 2.0.5 on 2018-07-19 08:33

from django.db import migrations, models
import django.db.models.deletion


def branch_unique_repo(apps, schema_editor):
    apps.get_model('rainboard', 'Branch').objects.filter(repo=None).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('rainboard', '0022_dependency_ros'),
    ]

    operations = [
        migrations.RunPython(branch_unique_repo),
        migrations.AlterField(
            model_name='branch',
            name='repo',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='rainboard.Repo'),
        ),
        migrations.AlterUniqueTogether(
            name='branch',
            unique_together={('project', 'name', 'repo')},
        ),
    ]
