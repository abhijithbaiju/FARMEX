from django.contrib import admin
from .models import Farmer, Company, Category, Scheme, Application, Notification, Feedback, State, District, SchemeLocation

admin.site.site_header = 'FARMEX Admin'
admin.site.site_title = 'FARMEX'
admin.site.index_title = 'FARMEX Administration'

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name']

@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ['name']

@admin.register(District)
class DistrictAdmin(admin.ModelAdmin):
    list_display = ['name', 'state']

@admin.register(SchemeLocation)
class SchemeLocationAdmin(admin.ModelAdmin):
    list_display = ['scheme', 'state', 'district']

@admin.register(Farmer)
class FarmerAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'phone', 'email_display', 'income', 'land_size']
    
    def email_display(self, obj):
        return obj.user.email
    email_display.short_description = 'Email'

@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ['company_name', 'company_registration_number', 'gst_number', 'is_approved']

@admin.register(Scheme)
class SchemeAdmin(admin.ModelAdmin):
    list_display = ['name', 'company', 'subsidy_amount']

@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ['application_number', 'farmer', 'scheme', 'status', 'submitted_at']

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['user', 'message', 'is_read', 'created_at']

@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ['farmer', 'company', 'rating', 'created_at']
