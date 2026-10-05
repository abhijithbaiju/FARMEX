from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
import os

LAND_AREA_UNITS = [
    ('sqm', 'Square Metre (m²)'),
    ('are', 'Are'),
    ('hectare', 'Hectare'),
    ('acre', 'Acre'),
    ('cent', 'Cent'),
    ('gunta', 'Gunta/Guntha'),
]

LAND_AREA_CONVERSIONS = {
    'sqm': Decimal('1'),
    'are': Decimal('100'),
    'hectare': Decimal('10000'),
    'acre': Decimal('4046.8564224'),
    'cent': Decimal('40.468564224'),
    'gunta': Decimal('101.17141056'),
}


def land_area_to_sqm(value, unit):
    if value is None or unit not in LAND_AREA_CONVERSIONS:
        return None
    return Decimal(str(value)) * LAND_AREA_CONVERSIONS[unit]

class Category(models.Model):
    name = models.CharField(max_length=50, unique=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name_plural = "Categories"


class State(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class District(models.Model):
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name='districts')
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['state', 'name'], name='unique_district_per_state'),
        ]

    def __str__(self):
        return f'{self.name}, {self.state.name}'

class Farmer(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    full_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=10, unique=True)
    aadhaar = models.CharField(max_length=12, unique=True)
    address = models.TextField()
    state = models.ForeignKey(State, on_delete=models.PROTECT, null=True, blank=True, related_name='farmers')
    district = models.ForeignKey(District, on_delete=models.PROTECT, null=True, blank=True, related_name='farmers')
    income = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    land_size = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    land_area_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, validators=[MinValueValidator(0)])
    land_area_unit = models.CharField(max_length=20, choices=LAND_AREA_UNITS, null=True, blank=True)
    land_area_sqm = models.DecimalField(max_digits=16, decimal_places=8, null=True, blank=True, validators=[MinValueValidator(0)])
    land_area_are = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, validators=[MinValueValidator(0)])
    land_area_sq_metre = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, validators=[MinValueValidator(0)])
    land_tax_receipt = models.FileField(upload_to='land_tax_receipts/', null=True, blank=True)
    categories = models.ManyToManyField(Category, related_name='farmers')
    redflag_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.full_name} - {self.phone}"
    
    def get_categories_list(self):
        return [cat.name for cat in self.categories.all()]

class Company(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    company_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=10, blank=True)
    company_registration_number = models.CharField(max_length=100, blank=True)
    gst_number = models.CharField(max_length=15, unique=True)
    country = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True)
    pincode = models.CharField(max_length=6, blank=True)
    registration_document = models.FileField(upload_to='company_docs/', null=True, blank=True)
    gst_registration_certificate = models.FileField(upload_to='company_docs/', null=True, blank=True)
    address_proof = models.FileField(upload_to='company_docs/', null=True, blank=True)
    is_approved = models.BooleanField(default=False)
    is_rejected = models.BooleanField(default=False)
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower('company_name'),
                condition=~Q(company_name=''),
                name='unique_company_name_case_insensitive',
            ),
            models.UniqueConstraint(
                Lower('company_registration_number'),
                condition=~Q(company_registration_number=''),
                name='unique_company_registration_case_insensitive',
            ),
            models.UniqueConstraint(
                'phone',
                condition=~Q(phone=''),
                name='unique_company_phone',
            ),
        ]
    
    def __str__(self):
        return self.company_name

class Scheme(models.Model):
    GEOGRAPHICAL_SCOPE_CHOICES = [
        ('all_states', 'All States'),
        ('selected_states', 'Selected States'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='schemes')
    name = models.CharField(max_length=200)
    description = models.TextField()
    application_deadline = models.DateField(null=True, blank=True)
    subsidy_amount = models.DecimalField(max_digits=10, decimal_places=2)
    actual_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    scheme_image = models.ImageField(upload_to='scheme_images/', null=True, blank=True)
    min_income = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_income = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    min_land = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    max_land = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    land_area_unit = models.CharField(max_length=20, choices=LAND_AREA_UNITS, default='sqm')
    min_land_sqm = models.DecimalField(max_digits=16, decimal_places=8, null=True, blank=True)
    max_land_sqm = models.DecimalField(max_digits=16, decimal_places=8, null=True, blank=True)
    geographical_scope = models.CharField(max_length=20, choices=GEOGRAPHICAL_SCOPE_CHOICES, default='all_states')
    eligible_categories = models.ManyToManyField(Category, related_name='schemes')
    custom_form_fields = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.min_land is not None:
            self.min_land_sqm = land_area_to_sqm(self.min_land, self.land_area_unit)
        if self.max_land is not None:
            self.max_land_sqm = land_area_to_sqm(self.max_land, self.land_area_unit)
        super().save(*args, **kwargs)


class SchemeLocation(models.Model):
    scheme = models.ForeignKey(Scheme, on_delete=models.CASCADE, related_name='locations')
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name='scheme_locations')
    district = models.ForeignKey(District, on_delete=models.CASCADE, null=True, blank=True, related_name='scheme_locations')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['scheme', 'state', 'district'], name='unique_scheme_location'),
        ]

    def __str__(self):
        return f'{self.scheme.name} - {self.district or self.state}'

def application_document_path(instance, filename):
    return f'application_docs/user_{instance.farmer.id}/{filename}'

class Application(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('fraud', 'Fraud Detected'),
    ]
    
    farmer = models.ForeignKey(Farmer, on_delete=models.CASCADE, related_name='applications')
    scheme = models.ForeignKey(Scheme, on_delete=models.CASCADE, related_name='applications')
    application_number = models.CharField(max_length=6, unique=True, editable=False, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    fraud_reason = models.TextField(blank=True, null=True)
    rejection_reason = models.TextField(blank=True, null=True)
    form_data = models.JSONField(default=dict)
    documents = models.JSONField(default=dict)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Feedback fields
    feedback_rating = models.IntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    feedback_comment = models.TextField(blank=True, null=True)
    feedback_photos = models.JSONField(default=list, blank=True)
    feedback_submitted_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.farmer.full_name} - {self.scheme.name} - {self.status}"

class Notification(models.Model):
    NOTIFICATION_TYPES = [
        ('info', 'Info'),
        ('success', 'Success'),
        ('error', 'Error'),
        ('warning', 'Warning'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    scheme = models.ForeignKey(Scheme, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')
    message = models.CharField(max_length=500)
    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES, default='info')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.user.username} - {self.message[:50]}"
    
    class Meta:
        ordering = ['-created_at']

class Feedback(models.Model):
    farmer = models.ForeignKey(Farmer, on_delete=models.CASCADE, related_name='feedbacks')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='feedbacks')
    application = models.OneToOneField(Application, on_delete=models.CASCADE, related_name='feedback')
    rating = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField()
    photos = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.farmer.full_name} -> {self.company.company_name}: {self.rating}★"
