from django.db import migrations


FARMING_CATEGORIES = [
    'Crop Farming',
    'Horticulture',
    'Animal Husbandry',
    'Fisheries',
    'Dairy',
    'Organic Farming',
    'Sericulture',
    'Poultry',
]


def seed_farming_categories(apps, schema_editor):
    Category = apps.get_model('accounts', 'Category')
    for name in FARMING_CATEGORIES:
        if not Category.objects.filter(name__iexact=name).exists():
            Category.objects.create(name=name)


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0015_scheme_application_deadline'),
    ]

    operations = [
        migrations.RunPython(seed_farming_categories, migrations.RunPython.noop),
    ]