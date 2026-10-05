from django.urls import path, reverse_lazy
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # Home
    path('', views.home, name='home'),
    
    # Authentication
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),
    path('register/farmer/', views.farmer_register, name='farmer_register'),
    path('register/company/', views.company_register, name='company_register'),
    path('password-reset/', auth_views.PasswordResetView.as_view(
        template_name='accounts/password_reset.html',
        email_template_name='accounts/password_reset_email.txt',
        subject_template_name='accounts/password_reset_subject.txt',
        success_url=reverse_lazy('password_reset_done')
    ), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='accounts/password_reset_done.html'
    ), name='password_reset_done'),
    path('password-reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='accounts/password_reset_confirm.html',
        success_url=reverse_lazy('password_reset_complete')
    ), name='password_reset_confirm'),
    path('password-reset/complete/', auth_views.PasswordResetCompleteView.as_view(
        template_name='accounts/password_reset_complete.html'
    ), name='password_reset_complete'),
    path('password-change/', views.password_change, name='password_change'),
    
    # Farmer
    path('farmer/dashboard/', views.farmer_dashboard, name='farmer_dashboard'),
    path('farmer/subsidy/<int:scheme_id>/', views.subsidy_details, name='subsidy_details'),
    path('farmer/apply/<int:scheme_id>/', views.apply_scheme, name='apply_scheme'),
    path('farmer/feedback/<int:application_id>/', views.give_feedback, name='give_feedback'),
    path('farmer/edit-profile/', views.edit_farmer_profile, name='edit_farmer_profile'),
    
    # Company
    path('company/dashboard/', views.company_dashboard, name='company_dashboard'),
    path('company/farmer-reviews/', views.company_farmer_reviews, name='company_farmer_reviews'),
    path('company/create-scheme/', views.create_scheme, name='create_scheme'),
    path('company/schemes/<int:scheme_id>/', views.scheme_details, name='scheme_details'),
    path('company/schemes/<int:scheme_id>/edit/', views.edit_scheme, name='edit_scheme'),
    path('company/schemes/<int:scheme_id>/delete/', views.delete_scheme, name='delete_scheme'),
    path('company/applications/', views.view_applications, name='view_applications'),
    path('company/applications/<int:scheme_id>/', views.view_applications, name='view_applications_scheme'),
    path('company/edit-profile/', views.edit_company_profile, name='edit_company_profile'),
    
    # Custom admin area (separate from Django admin)
    path('dashboard/admin/', views.admin_dashboard, name='admin_dashboard'),
    path('dashboard/admin/categories/', views.manage_category, name='manage_category'),
    path('dashboard/admin/farmer-reviews/', views.admin_farmer_reviews, name='admin_farmer_reviews'),
    path('dashboard/admin/approve-company/<int:company_id>/', views.approve_company, name='approve_company'),
    path('dashboard/admin/reject-company/<int:company_id>/', views.reject_company, name='reject_company'),
    path('dashboard/admin/block-company/<int:company_id>/', views.block_company, name='block_company'),
    path('dashboard/admin/remove-company/<int:company_id>/', views.remove_company, name='remove_company'),
    path('dashboard/admin/remove-farmer/<int:farmer_id>/', views.remove_farmer, name='remove_farmer'),
    path('dashboard/admin/block-farmer/<int:farmer_id>/', views.block_farmer, name='block_farmer'),
    path('dashboard/admin/block-scheme/<int:scheme_id>/', views.block_scheme, name='block_scheme'),
    
    # Notifications
    path('notifications/', views.notifications, name='notifications'),
    path('notification/read/<int:notification_id>/', views.mark_notification_read, name='mark_notification_read'),
]
