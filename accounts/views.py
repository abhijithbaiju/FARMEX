from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.paginator import Paginator
from django.utils.dateparse import parse_date
from django.db.models import Avg, Count, IntegerField, OuterRef, Q, Subquery
from django.db import IntegrityError, transaction
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.utils import timezone
from decimal import Decimal, InvalidOperation
import json
import hashlib
import os
import secrets

from .models import (
    Farmer, Company, Category, Scheme, Application,
    Notification, Feedback, State, District, SchemeLocation,
    LAND_AREA_UNITS, land_area_to_sqm
)
from .location_data import LOCATION_DATA
from django.contrib.auth.models import User

# Helper Functions
def add_notification(user, message, notification_type='info', scheme=None):
    Notification.objects.create(
        user=user,
        message=message,
        notification_type=notification_type,
        scheme=scheme,
    )


def location_context():
    states = State.objects.all()
    return {
        'states': states,
        'location_data': {
            str(state.id): list(state.districts.values('id', 'name'))
            for state in states
        },
    }


def scheme_matches_farmer(scheme, farmer):
    locations = scheme.locations.all()
    if scheme.geographical_scope == 'all_states':
        if locations.exists() and (not farmer.state_id or not locations.filter(state_id=farmer.state_id).exists()):
            return False
    else:
        if not farmer.state_id or not locations.exists():
            return False
        matching_locations = locations.filter(state_id=farmer.state_id)
        if not matching_locations.exists():
            return False
        if matching_locations.filter(district__isnull=True).exists():
            pass
        elif not matching_locations.filter(district_id=farmer.district_id).exists():
            return False

    min_income = Decimal(str(scheme.min_income)) if scheme.min_income is not None else None
    max_income = Decimal(str(scheme.max_income)) if scheme.max_income is not None else None
    farmer_income = Decimal(str(farmer.income)) if farmer.income is not None else None
    if min_income is not None and farmer_income < min_income:
        return False
    if max_income is not None and farmer_income > max_income:
        return False

    farmer_land_sqm = farmer.land_area_sqm or land_area_to_sqm(farmer.land_size, 'acre')
    min_land_sqm = Decimal(str(scheme.min_land_sqm)) if scheme.min_land_sqm is not None else None
    max_land_sqm = Decimal(str(scheme.max_land_sqm)) if scheme.max_land_sqm is not None else None
    if min_land_sqm is not None and farmer_land_sqm < min_land_sqm:
        return False
    if max_land_sqm is not None and farmer_land_sqm > max_land_sqm:
        return False
    if scheme.eligible_categories.exists() and not scheme.eligible_categories.filter(
        farmers=farmer
    ).exists():
        return False
    return True


def notify_eligible_farmers(scheme):
    for farmer in Farmer.objects.select_related('user', 'state', 'district').prefetch_related('categories'):
        if scheme_matches_farmer(scheme, farmer):
            add_notification(
                farmer.user,
                f"New subsidy scheme available: '{scheme.name}'",
                'info',
                scheme=scheme,
            )


def assign_application_number(application):
    """Assign a unique six-digit number to an already-saved application."""
    for _ in range(20):
        candidate = str(secrets.randbelow(900000) + 100000)
        if Application.objects.filter(application_number=candidate).exists():
            continue
        application.application_number = candidate
        try:
            with transaction.atomic():
                application.save(update_fields=['application_number'])
            return candidate
        except IntegrityError:
            application.application_number = None
    raise RuntimeError('Unable to generate a unique application number')

def is_farmer(user):
    return hasattr(user, 'farmer')

def is_company(user):
    return hasattr(user, 'company')

def is_admin(user):
    return user.is_superuser

def check_fraud(application):
    """Check for fraud in application"""
    farmer = application.farmer
    
    # Check 1: Multiple applications in one day
    today_apps = Application.objects.filter(
        farmer=farmer,
        submitted_at__date=timezone.now().date()
    ).count()
    if today_apps >= 3:
        return "More than 3 applications in one day"
    
    # Check 2: Unrealistic data combination
    if farmer.land_area_sqm and farmer.land_area_sqm > Decimal('202342.82112') and farmer.income < 50000:
        return "Land size too large for reported income"
    
    # Check 3: Already applied for same scheme
    if Application.objects.filter(farmer=farmer, scheme=application.scheme).exclude(id=application.id).exists():
        return "Duplicate application for same scheme"
    
    return None

# Authentication Views
def home(request):
    return render(request, 'accounts/home.html')

def user_login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            if hasattr(user, 'farmer'):
                login(request, user)
                return redirect('farmer_dashboard')
            elif hasattr(user, 'company') and user.company.is_approved:
                login(request, user)
                return redirect('company_dashboard')
            elif user.is_superuser:
                login(request, user)
                return redirect('admin_dashboard')
            else:
                messages.error(request, 'Your company account is pending approval')
                return redirect('login')
        else:
            messages.error(request, 'Invalid username or password')
    return render(request, 'accounts/login.html')

def user_logout(request):
    logout(request)
    return redirect('home')


@login_required
def password_change(request):
    form = PasswordChangeForm(user=request.user, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(request, 'Your password has been changed successfully.')
        if hasattr(user, 'farmer'):
            return redirect('farmer_dashboard')
        if hasattr(user, 'company'):
            return redirect('company_dashboard')
        if user.is_superuser:
            return redirect('admin_dashboard')
        return redirect('home')

    return render(request, 'accounts/password_change.html', {'form': form})

# Farmer Views
def farmer_register(request):
    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')
        aadhaar = request.POST.get('aadhaar', '').strip()
        country = request.POST.get('country', '').strip()
        state_id = request.POST.get('state', '').strip()
        district_id = request.POST.get('district', '').strip()
        house_address = request.POST.get('house_address', '').strip()
        pincode = request.POST.get('pincode', '').strip()
        income = request.POST.get('income', '').strip()
        land_area_value = request.POST.get('land_area_value', '').strip()
        land_area_unit = request.POST.get('land_area_unit', '').strip()
        land_area_are = request.POST.get('land_area_are', '').strip()
        land_area_sq_metre = request.POST.get('land_area_sq_metre', '').strip()
        land_tax_receipt = request.FILES.get('land_tax_receipt')
        selected_categories = request.POST.getlist('categories')
        
        errors = []
        
        state_lookup = {'id': state_id} if state_id.isdigit() else {'name': state_id}
        state = State.objects.filter(**state_lookup).first()
        if district_id.isdigit():
            district_lookup = {'id': district_id}
        else:
            district_lookup = {'name': district_id}
        district = District.objects.filter(state=state, **district_lookup).first() if state else None

        if not all([full_name, phone, email, password, aadhaar, country, state, district, house_address, pincode, income]):
            errors.append("All fields are required")
        if state_id and not state:
            errors.append("Please select a valid state")
        if district_id and not district:
            errors.append("Please select a district belonging to the selected state")
        
        if len(phone) != 10 or not phone.isdigit():
            errors.append("Phone number must be exactly 10 digits")
        
        if len(full_name) > 100:
            errors.append("Full name cannot exceed 100 characters")

        if phone and Farmer.objects.filter(phone=phone).exists():
            errors.append("Phone number already registered")

        if email:
            email = User.objects.normalize_email(email)
            if len(email) > 150:
                errors.append("Email cannot exceed 150 characters because it is used as the login name")
            try:
                validate_email(email)
            except ValidationError:
                errors.append("Enter a valid email address")

        if email and (
            User.objects.filter(email__iexact=email).exists()
            or User.objects.filter(username__iexact=email).exists()
        ):
            errors.append("Email already registered")
        
        if password != confirm_password:
            errors.append("Passwords do not match")
        
        if password:
            try:
                validate_password(
                    password,
                    user=User(username=email, email=email, first_name=full_name),
                )
            except ValidationError as error:
                errors.extend(error.messages)
        
        if len(aadhaar) != 12 or not aadhaar.isdigit():
            errors.append("Aadhaar must be exactly 12 digits")
        
        if aadhaar and Farmer.objects.filter(aadhaar=aadhaar).exists():
            errors.append("Aadhaar number already registered")
        
        if len(house_address) < 3:
            errors.append("Please enter a valid house name or house number")

        if not pincode.isdigit() or len(pincode) != 6:
            errors.append("Pincode must be exactly 6 digits")

        address = f"{house_address}, {district.name if district else district_id}, {state.name if state else state_id}, {country} - {pincode}"
        
        try:
            income_value = Decimal(income)
            if income_value < 0:
                errors.append("Income cannot be negative")
        except (InvalidOperation, TypeError):
            errors.append("Income must be a valid number")

        area_value = None
        area_sqm = None
        if land_area_value or land_area_unit:
            try:
                area_value = Decimal(land_area_value)
                if area_value <= 0:
                    errors.append("Land area must be greater than zero")
                elif area_value > Decimal('100000000'):
                    errors.append("Land area is unreasonably large")
                elif land_area_unit not in dict(LAND_AREA_UNITS):
                    errors.append("Please select a valid land area unit")
                else:
                    area_sqm = land_area_to_sqm(area_value, land_area_unit)
            except (InvalidOperation, TypeError):
                errors.append("Land area must be a positive number")

        compound_are_value = None
        compound_sqm_value = None
        if land_area_are or land_area_sq_metre:
            try:
                compound_are_value = Decimal(land_area_are)
                compound_sqm_value = Decimal(land_area_sq_metre)
                if compound_are_value <= 0 or compound_sqm_value < 0:
                    errors.append("Are must be positive and square metres cannot be negative")
                elif compound_are_value > Decimal('1000000') or compound_sqm_value > Decimal('100000000'):
                    errors.append("Compound land area is unreasonably large")
                else:
                    area_sqm = (compound_are_value * Decimal('100')) + compound_sqm_value
                    area_value = area_sqm
                    land_area_unit = 'sqm'
            except (InvalidOperation, TypeError):
                errors.append("Are and square metres must be valid numbers")
        elif not land_area_value or not land_area_unit:
            errors.append("Enter a land area value and unit, or use the Kerala Are + Square Metre format")
        
        if not selected_categories:
            errors.append("Please select at least one farming category")

        category_records = Category.objects.filter(name__in=selected_categories)
        if len(set(selected_categories)) != category_records.count():
            errors.append("Please select valid farming categories")
        
        if errors:
            for error in errors:
                messages.error(request, error)
            context = {
                'categories': Category.objects.order_by('name'),
                'selected_categories': selected_categories,
                **location_context(),
                'selected_state': state_id,
                'selected_district': district_id,
            }
            return render(request, 'accounts/farmer_register.html', context)
        
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=email,
                    email=email,
                    password=password,
                    first_name=full_name
                )

                farmer = Farmer.objects.create(
                    user=user,
                    full_name=full_name,
                    phone=phone,
                    aadhaar=aadhaar,
                    address=address,
                    state=state,
                    district=district,
                    income=income,
                    land_size=(area_sqm / Decimal('4046.8564224')).quantize(Decimal('0.01')),
                    land_area_value=area_value,
                    land_area_unit=land_area_unit,
                    land_area_sqm=area_sqm,
                    land_area_are=compound_are_value,
                    land_area_sq_metre=compound_sqm_value,
                    land_tax_receipt=land_tax_receipt,
                )
        except IntegrityError:
            messages.error(request, 'An account with this email, phone, or Aadhaar number already exists.')
            context = {
                'categories': Category.objects.order_by('name'),
                'selected_categories': selected_categories,
                **location_context(),
                'selected_state': state_id,
                'selected_district': district_id,
            }
            return render(request, 'accounts/farmer_register.html', context)
        
        farmer.categories.set(category_records)
        
        add_notification(user, f"Welcome {full_name}! Registration successful.", 'success')
        messages.success(request, 'Registration successful! Please log in.')
        return redirect('login')
    
    context = {
        'categories': Category.objects.order_by('name'),
        'selected_categories': [],
        **location_context(),
    }
    return render(request, 'accounts/farmer_register.html', context)

@login_required
@user_passes_test(is_farmer)
def farmer_dashboard(request):
    farmer = request.user.farmer
    farmer_categories = farmer.categories.all()
    farmer_land_area_sqm = farmer.land_area_sqm or land_area_to_sqm(farmer.land_size, 'acre')
    
    eligible_schemes = Scheme.objects.filter(
        is_active=True,
        eligible_categories__in=farmer_categories
    ).distinct()
    
    eligible_schemes = eligible_schemes.filter(
        Q(min_income__isnull=True) | Q(min_income__lte=farmer.income),
        Q(max_income__isnull=True) | Q(max_income__gte=farmer.income),
        Q(min_land_sqm__isnull=True) | Q(min_land_sqm__lte=farmer_land_area_sqm),
        Q(max_land_sqm__isnull=True) | Q(max_land_sqm__gte=farmer_land_area_sqm),
    ).filter(
        Q(geographical_scope='all_states')
        | (
            Q(locations__state_id=farmer.state_id)
            & (Q(locations__district__isnull=True) | Q(locations__district_id=farmer.district_id))
        )
    ).distinct().order_by('-created_at', '-id')

    scheme_feedback = Feedback.objects.filter(
        application__scheme_id=OuterRef('pk')
    ).order_by()
    scheme_feedback_average = scheme_feedback.values(
        'application__scheme_id'
    ).annotate(
        average=Avg('rating')
    ).values('average')[:1]
    scheme_feedback_count = scheme_feedback.values(
        'application__scheme_id'
    ).annotate(
        total=Count('id')
    ).values('total')[:1]
    eligible_schemes = eligible_schemes.annotate(
        feedback_average=Subquery(scheme_feedback_average),
        feedback_count=Subquery(scheme_feedback_count, output_field=IntegerField()),
    )
    
    applications = farmer.applications.all().order_by('-submitted_at')
    feedbacks = farmer.feedbacks.all().order_by('-created_at')
    unread_notifications_count = Notification.objects.filter(user=request.user, is_read=False).count()

    # Convert active DB eligible schemes into card objects
    category_colors = {
        'crop': ('Crop', 'badge-crop', 'images/hybrid_paddy_seeds.jpg'),
        'grain': ('Crop', 'badge-crop', 'images/hybrid_paddy_seeds.jpg'),
        'livestock': ('Livestock', 'badge-livestock', 'images/dairy_feed_subsidy.jpg'),
        'dairy': ('Livestock', 'badge-livestock', 'images/dairy_feed_subsidy.jpg'),
        'equipment': ('Equipment', 'badge-equipment', 'images/tractor_purchase_support.jpg'),
        'machinery': ('Equipment', 'badge-equipment', 'images/tractor_purchase_support.jpg'),
        'fertilizer': ('Fertilizer', 'badge-crop', 'images/hybrid_paddy_seeds.jpg'),
    }

    applied_scheme_ids = set(applications.values_list('scheme_id', flat=True))

    display_cards = []
    for s in eligible_schemes:
        first_cat = s.eligible_categories.first()
        cat_name = first_cat.name if first_cat else 'General'
        key = (cat_name + ' ' + s.name).lower()
        badge_title = cat_name
        badge_cls = 'badge-crop'
        img_src = 'images/hybrid_paddy_seeds.jpg'
        for k, v in category_colors.items():
            if k in key or k in s.name.lower():
                badge_title, badge_cls, img_src = v
                break
        
        # Determine actual price and formatted subsidy
        sub_amt = float(s.subsidy_amount)
        est_actual = sub_amt * 1.35
        actual_fmt = f"₹{s.actual_price:,.0f}" if s.actual_price is not None else f"₹{est_actual:,.0f}"
        subsidy_fmt = f"₹{sub_amt:,.0f}"

        display_cards.append({
            'id': s.id,
            'title': s.name,
            'category': badge_title,
            'category_class': badge_cls,
            'company_name': s.company.company_name,
            'feedback_average': s.feedback_average,
            'feedback_count': s.feedback_count or 0,
            'valid_till': s.application_deadline.strftime('%d %b %Y') if s.application_deadline else 'No deadline set',
            'actual_price': actual_fmt,
            'subsidy_price': subsidy_fmt,
            'image': s.scheme_image.url if s.scheme_image else img_src,
            'already_applied': s.id in applied_scheme_ids,
            'application_closed': bool(
                s.application_deadline and s.application_deadline < timezone.localdate()
            ),
            'min_income': s.min_income,
            'max_income': s.max_income,
            'min_land': s.min_land_sqm,
            'max_land': s.max_land_sqm,
        })

    context = {
        'farmer': farmer,
        'eligible_schemes': eligible_schemes,
        'applications': applications,
        'feedbacks': feedbacks,
        'farmer_categories': farmer_categories,
        'unread_notifications_count': unread_notifications_count,
        'display_cards': display_cards,
    }
    return render(request, 'accounts/farmer_dashboard.html', context)


@login_required
@user_passes_test(is_farmer)
def apply_scheme(request, scheme_id):
    scheme = get_object_or_404(Scheme, id=scheme_id)
    farmer = request.user.farmer

    if scheme.application_deadline and scheme.application_deadline < timezone.localdate():
        messages.error(request, 'The application period for this scheme has closed.')
        return redirect('farmer_dashboard')

    if not scheme_matches_farmer(scheme, farmer):
        messages.error(request, 'You are not eligible for this subsidy scheme based on your registered location or eligibility criteria.')
        return redirect('farmer_dashboard')
    
    if request.method == 'POST':
        required_documents = ['aadhaar_card', 'income_certificate', 'land_tax_document']
        missing_documents = [
            doc_name for doc_name in required_documents
            if not request.FILES.get(doc_name)
        ]

        if missing_documents:
            messages.error(request, 'Aadhaar card, income certificate, and land tax document are mandatory.')
            return render(request, 'accounts/apply_scheme.html', {'scheme': scheme})

        form_data = {}
        documents = {}
        
        for key, value in request.POST.items():
            if key not in ['csrfmiddlewaretoken']:
                form_data[key] = value
        
        for key, file in request.FILES.items():
            file_path = default_storage.save(
                f'application_docs/farmer_{farmer.id}/scheme_{scheme.id}/{file.name}',
                ContentFile(file.read())
            )
            documents[key] = file_path
        
        with transaction.atomic():
            application = Application.objects.create(
                farmer=farmer,
                scheme=scheme,
                form_data=form_data,
                documents=documents
            )
            application_number = assign_application_number(application)

            fraud_reason = check_fraud(application)
            if fraud_reason:
                application.status = 'fraud'
                application.fraud_reason = fraud_reason
                application.save(update_fields=['status', 'fraud_reason'])
                add_notification(request.user, f"Fraud detected: {fraud_reason}. Application {application_number} terminated.", 'error')
                messages.error(request, f"Fraud detected: {fraud_reason}. Application Number: {application_number}")
            else:
                company_user = scheme.company.user
                add_notification(company_user, f"New application {application_number} received for scheme '{scheme.name}' from {farmer.full_name}", 'info')
                add_notification(request.user, f"Application submitted successfully for '{scheme.name}'. Application Number: {application_number}", 'success')
                messages.success(request, f"Application Submitted Successfully. Application Number: {application_number}. Status: Pending Verification")
        
        return redirect('farmer_dashboard')
    
    context = {'scheme': scheme}
    return render(request, 'accounts/apply_scheme.html', context)


@login_required
@user_passes_test(is_farmer)
def subsidy_details(request, scheme_id):
    scheme = get_object_or_404(Scheme, id=scheme_id, is_active=True)
    farmer = request.user.farmer
    if not scheme_matches_farmer(scheme, farmer):
        messages.error(request, 'You are not eligible for this subsidy scheme.')
        return redirect('farmer_dashboard')
    locations = scheme.locations.select_related('state', 'district').order_by('state__name', 'district__name')
    scheme_feedback = Feedback.objects.filter(
        application__scheme=scheme
    ).select_related('farmer').order_by('-created_at', '-id')
    feedback_summary = scheme_feedback.aggregate(
        average_rating=Avg('rating'),
        review_count=Count('id'),
    )
    review_cards = [
        {
            'feedback': feedback,
            'stars': '\u2605' * feedback.rating + '\u2606' * (5 - feedback.rating),
        }
        for feedback in scheme_feedback
    ]
    context = {
        'scheme': scheme,
        'feedback_average': feedback_summary['average_rating'],
        'feedback_count': feedback_summary['review_count'],
        'scheme_review_cards': review_cards,
        'application_closed': bool(
            scheme.application_deadline and scheme.application_deadline < timezone.localdate()
        ),
        'scheme_locations': locations,
        'has_district_locations': locations.filter(district__isnull=False).exists(),
        'has_state_level_locations': locations.filter(district__isnull=True).exists(),
        'already_applied': Application.objects.filter(farmer=farmer, scheme=scheme).exists(),
    }
    return render(request, 'accounts/subsidy_details.html', context)


@login_required
@user_passes_test(is_farmer)
def give_feedback(request, application_id):
    application = get_object_or_404(Application, id=application_id, farmer=request.user.farmer)
    
    if application.status not in ['approved', 'rejected']:
        messages.error(request, "Feedback can only be given after company decision")
        return redirect('farmer_dashboard')
    
    if hasattr(application, 'feedback'):
        messages.error(request, "Feedback already given")
        return redirect('farmer_dashboard')
    
    if request.method == 'POST':
        rating = request.POST.get('rating')
        comment = request.POST.get('comment')
        uploaded_photos = []

        for uploaded_file in request.FILES.getlist('photos'):
            file_path = default_storage.save(
                f'feedback_photos/farmer_{request.user.farmer.id}/application_{application.id}/{uploaded_file.name}',
                ContentFile(uploaded_file.read())
            )
            uploaded_photos.append(file_path)

        Feedback.objects.create(
            farmer=request.user.farmer,
            company=application.scheme.company,
            application=application,
            rating=rating,
            comment=comment,
            photos=uploaded_photos,
        )

        add_notification(
            application.scheme.company.user,
            f"New feedback received from {request.user.farmer.full_name} for scheme '{application.scheme.name}'",
            'info'
        )

        messages.success(request, 'Thank you for your feedback!')
        return redirect('farmer_dashboard')
    
    context = {'application': application}
    return render(request, 'accounts/feedback.html', context)

# Company Views
def company_register(request):
    if request.method == 'POST':
        company_name = request.POST.get('company_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        company_registration_number = request.POST.get('company_registration_number', '').strip().upper()
        gst_number = request.POST.get('gst_number', '').strip().upper()
        country = request.POST.get('country', '').strip()
        state = request.POST.get('state', '').strip()
        address = request.POST.get('address', '').strip()
        pincode = request.POST.get('pincode', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')
        registration_document = request.FILES.get('registration_document')
        gst_registration_certificate = request.FILES.get('gst_registration_certificate')
        address_proof = request.FILES.get('address_proof')
        
        errors = []
        
        if not all([
            company_name, phone, company_registration_number, gst_number, country,
            state, address, pincode, email, password, confirm_password,
            registration_document, gst_registration_certificate, address_proof,
        ]):
            errors.append("All fields are required")

        if not pincode.isdigit() or len(pincode) != 6:
            errors.append("Pincode must be exactly 6 digits")

        if len(phone) != 10 or not phone.isdigit():
            errors.append("Phone number must be exactly 10 digits")

        if len(company_name) > 200:
            errors.append("Company name cannot exceed 200 characters")
        if len(company_registration_number) > 100:
            errors.append("Company registration number cannot exceed 100 characters")
        if len(gst_number) > 15:
            errors.append("GST number cannot exceed 15 characters")
        if len(country) > 100 or len(state) > 100:
            errors.append("Country and state cannot exceed 100 characters")

        if email:
            email = User.objects.normalize_email(email)
            if len(email) > 150:
                errors.append("Email cannot exceed 150 characters because it is used as the login name")
            try:
                validate_email(email)
            except ValidationError:
                errors.append("Enter a valid email address")

        if company_name and Company.objects.filter(company_name__iexact=company_name).exists():
            errors.append("Company name already registered")

        if company_registration_number and Company.objects.filter(
            company_registration_number__iexact=company_registration_number
        ).exists():
            errors.append("Company registration number already registered")

        if phone and Company.objects.filter(phone=phone).exists():
            errors.append("Phone number already registered")
        
        if gst_number and Company.objects.filter(gst_number__iexact=gst_number).exists():
            errors.append("GST number already registered")
        
        if email and User.objects.filter(email__iexact=email).exists():
            errors.append("Email already registered")
        
        if password != confirm_password:
            errors.append("Passwords do not match")
        
        if password:
            try:
                validate_password(password, user=User(username=email, email=email))
            except ValidationError as error:
                errors.extend(error.messages)
        
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'accounts/company_register.html')
        
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=email,
                    email=email,
                    password=password
                )

                company = Company.objects.create(
                    user=user,
                    company_name=company_name,
                    phone=phone,
                    company_registration_number=company_registration_number,
                    gst_number=gst_number,
                    country=country,
                    state=state,
                    address=address,
                    pincode=pincode,
                )
        except IntegrityError:
            messages.error(request, 'A company with one or more of these registration details already exists.')
            return render(request, 'accounts/company_register.html')
        
        for field_name, uploaded_file in [
            ('registration_document', registration_document),
            ('gst_registration_certificate', gst_registration_certificate),
            ('address_proof', address_proof),
        ]:
            doc_path = default_storage.save(
                f'company_docs/{company.id}/{uploaded_file.name}',
                ContentFile(uploaded_file.read())
            )
            setattr(company, field_name, doc_path)
        company.save()
        
        admin_users = User.objects.filter(is_superuser=True)
        for admin in admin_users:
            add_notification(admin, f"New company registration: {company_name}. Please verify.", 'warning')
        
        messages.success(request, 'Registration submitted! Waiting for admin approval.')
        return redirect('login')
    
    return render(request, 'accounts/company_register.html')

@login_required
@user_passes_test(is_company)
def company_dashboard(request):
    company = request.user.company
    
    if not company.is_approved:
        messages.warning(request, 'Your account is pending admin approval.')
        return render(request, 'accounts/company_dashboard.html', {'company': company, 'pending': True})
    
    schemes = company.schemes.all().order_by('-created_at')
    applications = Application.objects.filter(scheme__in=schemes).order_by('-submitted_at')
    company_feedback = Feedback.objects.filter(
        company=company,
        application__scheme__company=company,
    )
    farmer_rating_summary = company_feedback.aggregate(
        average=Avg('rating'),
        total=Count('id'),
    )
    
    stats = {
        'total_schemes': schemes.count(),
        'total_applications': applications.count(),
        'pending_applications': applications.filter(status='pending').count(),
        'approved_applications': applications.filter(status='approved').count(),
        'rejected_applications': applications.filter(status='rejected').count(),
        'farmer_rating': farmer_rating_summary['average'],
        'farmer_review_count': farmer_rating_summary['total'],
    }

    # Presentation helpers for the company's actual existing schemes
    category_colors = {
        'crop': ('Crop', 'badge-crop', 'images/hybrid_paddy_seeds.jpg'),
        'grain': ('Crop', 'badge-crop', 'images/hybrid_paddy_seeds.jpg'),
        'fertilizer': ('Crop', 'badge-crop', 'images/hybrid_paddy_seeds.jpg'),
        'organic': ('Crop', 'badge-crop', 'images/hybrid_paddy_seeds.jpg'),
        'livestock': ('Livestock', 'badge-livestock', 'images/dairy_feed_subsidy.jpg'),
        'dairy': ('Livestock', 'badge-livestock', 'images/dairy_feed_subsidy.jpg'),
        'equipment': ('Equipment', 'badge-equipment', 'images/tractor_purchase_support.jpg'),
        'machinery': ('Equipment', 'badge-equipment', 'images/tractor_purchase_support.jpg'),
        'tractor': ('Equipment', 'badge-equipment', 'images/tractor_purchase_support.jpg'),
    }

    enriched_schemes = []
    for s in schemes:
        first_cat = s.eligible_categories.first()
        cat_name = first_cat.name if first_cat else 'General'
        key = (cat_name + ' ' + s.name).lower()
        badge_title = cat_name
        badge_cls = 'badge-crop'
        img_src = 'images/hybrid_paddy_seeds.jpg'

        for k, v in category_colors.items():
            if k in key:
                badge_title, badge_cls, img_src = v
                break

        sub_amt = float(s.subsidy_amount)
        est_actual = sub_amt * 1.35
        actual_fmt = f"₹{s.actual_price:,.0f}" if s.actual_price is not None else f"₹{est_actual:,.0f}"
        subsidy_fmt = f"₹{sub_amt:,.0f}"

        enriched_schemes.append({
            'obj': s,
            'id': s.id,
            'name': s.name,
            'description': s.description,
            'category': badge_title,
            'category_class': badge_cls,
            'image': s.scheme_image.url if s.scheme_image else img_src,
            'actual_price': actual_fmt,
            'subsidy_price': subsidy_fmt,
            'applications_count': s.applications.count(),
        })

    unread_notification_count = Notification.objects.filter(user=request.user, is_read=False).count()
    if unread_notification_count == 0:
        unread_notification_count = 4

    context = {
        'company': company,
        'schemes': schemes,
        'enriched_schemes': enriched_schemes,
        'applications': applications[:20],
        'stats': stats,
        'unread_notification_count': unread_notification_count,
    }
    return render(request, 'accounts/company_dashboard.html', context)


@login_required
@user_passes_test(is_company)
def company_farmer_reviews(request):
    company = request.user.company
    company_schemes = Scheme.objects.filter(company=company).order_by('name')
    reviews = Feedback.objects.filter(
        company=company,
        application__scheme__company=company,
    ).select_related(
        'application__scheme', 'company'
    ).order_by('-created_at', '-id')

    selected_scheme = request.GET.get('scheme', '').strip()
    if selected_scheme.isdigit() and company_schemes.filter(id=int(selected_scheme)).exists():
        reviews = reviews.filter(application__scheme_id=int(selected_scheme))
    else:
        selected_scheme = ''

    selected_rating = request.GET.get('rating', '').strip()
    if selected_rating in {'1', '2', '3', '4', '5'}:
        reviews = reviews.filter(rating=int(selected_rating))
    else:
        selected_rating = ''

    company_feedback = Feedback.objects.filter(
        company=company,
        application__scheme__company=company,
    )
    overall = company_feedback.aggregate(average=Avg('rating'), total=Count('id'))
    rating_counts = dict(
        company_feedback.values('rating').annotate(total=Count('id')).values_list('rating', 'total')
    )
    rating_distribution = [
        {
            'rating': rating,
            'stars': '\u2605' * rating + '\u2606' * (5 - rating),
            'count': rating_counts.get(rating, 0),
        }
        for rating in range(5, 0, -1)
    ]

    paginator = Paginator(reviews, 9)
    page_obj = paginator.get_page(request.GET.get('page'))
    review_cards = [
        {
            'feedback': feedback,
            'stars': '\u2605' * feedback.rating + '\u2606' * (5 - feedback.rating),
        }
        for feedback in page_obj.object_list
    ]
    page_numbers = range(
        max(1, page_obj.number - 2),
        min(paginator.num_pages, page_obj.number + 2) + 1,
    ) if paginator.num_pages else []

    return render(request, 'accounts/company_farmer_reviews.html', {
        'company': company,
        'company_schemes': company_schemes,
        'review_cards': review_cards,
        'page_obj': page_obj,
        'page_numbers': page_numbers,
        'overall_rating': overall['average'],
        'review_count': overall['total'],
        'rating_distribution': rating_distribution,
        'selected_scheme': selected_scheme,
        'selected_rating': selected_rating,
    })

@login_required
@user_passes_test(is_company)
def create_scheme(request):
    if not request.user.company.is_approved:
        messages.error(request, 'Your company is not approved yet')
        return redirect('company_dashboard')

    categories = Category.objects.order_by('name')

    if request.method == 'POST':
        all_states_selected = request.POST.get('all_states') == '1'
        selected_state_ids = [int(value) for value in request.POST.getlist('eligible_states') if value.isdigit()]
        selected_district_ids = [int(value) for value in request.POST.getlist('eligible_districts') if value.isdigit()]
        geographical_scope = 'all_states' if all_states_selected else 'selected_states'
        selected_states = State.objects.filter(id__in=selected_state_ids) if not all_states_selected else State.objects.all()
        errors = []

        deadline_raw = request.POST.get('application_deadline', '').strip()
        application_deadline = parse_date(deadline_raw) if deadline_raw else None
        if not deadline_raw:
            errors.append('Last date for applications is required.')
        elif application_deadline is None:
            errors.append('Enter a valid application deadline.')
        elif application_deadline < timezone.localdate():
            errors.append('Application deadline cannot be in the past. Set a future date to extend the application period.')

        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        deadline_raw = request.POST.get('application_deadline', '').strip()
        application_deadline = parse_date(deadline_raw) if deadline_raw else None
        if not deadline_raw:
            errors.append('Last date for applications is required.')
        elif application_deadline is None:
            errors.append('Enter a valid application deadline.')
        elif application_deadline < timezone.localdate():
            errors.append('Application deadline cannot be in the past.')

        decimal_values = {}
        for field_name, label, required in (
            ('subsidy_amount', 'Subsidy amount', True),
            ('actual_price', 'Actual product price', False),
            ('min_income', 'Minimum income', False),
            ('max_income', 'Maximum income', False),
            ('min_land', 'Minimum land area', False),
            ('max_land', 'Maximum land area', False),
        ):
            raw_value = request.POST.get(field_name, '').strip()
            if not raw_value:
                if required:
                    errors.append(f'{label} is required.')
                decimal_values[field_name] = None
                continue
            try:
                value = Decimal(raw_value)
            except (InvalidOperation, ValueError):
                errors.append(f'{label} must be a valid number.')
                decimal_values[field_name] = None
                continue
            if value < 0:
                errors.append(f'{label} cannot be negative.')
            decimal_values[field_name] = value

        if not name:
            errors.append('Scheme name is required.')
        if not description:
            errors.append('Description is required.')
        if decimal_values.get('min_income') is not None and decimal_values.get('max_income') is not None and decimal_values['min_income'] > decimal_values['max_income']:
            errors.append('Minimum income cannot be greater than maximum income.')
        if decimal_values.get('min_land') is not None and decimal_values.get('max_land') is not None and decimal_values['min_land'] > decimal_values['max_land']:
            errors.append('Minimum land area cannot be greater than maximum land area.')

        if all_states_selected:
            selected_state_ids = list(State.objects.values_list('id', flat=True))
        elif selected_states.count() == 0:
            errors.append('Please select at least one eligible state or choose All States.')
        if not all_states_selected and len(selected_states) != len(set(selected_state_ids)):
            errors.append('One or more selected states are invalid.')
        if not all_states_selected:
            invalid_district_ids = list(District.objects.filter(id__in=selected_district_ids).exclude(state_id__in=selected_state_ids).values_list('id', flat=True))
            if invalid_district_ids:
                errors.append('One or more selected districts do not belong to the chosen states.')

        selected_categories = Category.objects.filter(id__in=request.POST.getlist('eligible_categories'))
        if not selected_categories.exists():
            errors.append('Please select at least one eligible category.')

        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'accounts/create_scheme.html', {
                'categories': categories,
                'scheme': None,
                'selected_state_ids': set(selected_state_ids),
                'selected_district_ids': set(selected_district_ids),
                'selected_category_ids': set(request.POST.getlist('eligible_categories')),
                'geographical_scope': geographical_scope,
                'application_deadline_value': deadline_raw,
                **location_context(),
            })

        scheme = Scheme.objects.create(
            company=request.user.company,
            name=name,
            description=description,
            application_deadline=application_deadline,
            subsidy_amount=decimal_values['subsidy_amount'],
            min_income=decimal_values['min_income'],
            max_income=decimal_values['max_income'],
            min_land=decimal_values['min_land'],
            max_land=decimal_values['max_land'],
            land_area_unit=request.POST.get('land_area_unit') or 'sqm',
            actual_price=decimal_values['actual_price'],
            scheme_image=request.FILES.get('scheme_image'),
            geographical_scope=geographical_scope,
        )

        scheme.eligible_categories.set(selected_categories)

        scheme.locations.all().delete()
        if geographical_scope == 'all_states':
            rows = [SchemeLocation(scheme=scheme, state=state, district=None) for state in State.objects.all()]
            if rows:
                SchemeLocation.objects.bulk_create(rows)
        else:
            selected_district_lookup = {}
            for district in District.objects.filter(id__in=selected_district_ids):
                selected_district_lookup.setdefault(district.state_id, []).append(district.id)

            rows = []
            for state in selected_states:
                district_ids = selected_district_lookup.get(state.id, [])
                if district_ids:
                    rows.extend(
                        SchemeLocation(scheme=scheme, state=state, district_id=district_id)
                        for district_id in sorted(district_ids)
                    )
                else:
                    rows.append(SchemeLocation(scheme=scheme, state=state, district=None))
            if rows:
                SchemeLocation.objects.bulk_create(rows)

        notify_eligible_farmers(scheme)
        add_notification(request.user, f"Scheme '{scheme.name}' created successfully!", 'success')
        messages.success(request, 'Scheme created successfully!')
        return redirect('company_dashboard')

    context = {'categories': categories, 'geographical_scope': 'all_states', **location_context()}
    return render(request, 'accounts/create_scheme.html', context)


@login_required
@user_passes_test(is_company)
def scheme_details(request, scheme_id):
    scheme = get_object_or_404(Scheme, id=scheme_id, company=request.user.company)
    return render(request, 'accounts/scheme_details.html', {
        'scheme': scheme,
        'application_closed': bool(
            scheme.application_deadline and scheme.application_deadline < timezone.localdate()
        ),
    })


@login_required
@user_passes_test(is_company)
def edit_scheme(request, scheme_id):
    scheme = get_object_or_404(Scheme, id=scheme_id, company=request.user.company)
    categories = Category.objects.all()

    if request.method == 'POST':
        all_states_selected = request.POST.get('all_states') == '1'
        selected_state_ids = [int(value) for value in request.POST.getlist('eligible_states') if value.isdigit()]
        selected_district_ids = [int(value) for value in request.POST.getlist('eligible_districts') if value.isdigit()]
        geographical_scope = 'all_states' if all_states_selected else 'selected_states'
        selected_states = State.objects.filter(id__in=selected_state_ids) if not all_states_selected else State.objects.all()
        errors = []

        deadline_raw = request.POST.get('application_deadline', '').strip()
        application_deadline = parse_date(deadline_raw) if deadline_raw else None
        if not deadline_raw:
            errors.append('Last date for applications is required.')
        elif application_deadline is None:
            errors.append('Enter a valid application deadline.')
        elif application_deadline < timezone.localdate():
            errors.append('Application deadline cannot be in the past. Set a future date to extend the application period.')

        if all_states_selected:
            selected_state_ids = list(State.objects.values_list('id', flat=True))
        elif selected_states.count() == 0:
            errors.append('Please select at least one eligible state or choose All States.')
        if not all_states_selected and len(selected_states) != len(set(selected_state_ids)):
            errors.append('One or more selected states are invalid.')
        if not all_states_selected:
            invalid_district_ids = list(District.objects.filter(id__in=selected_district_ids).exclude(state_id__in=selected_state_ids).values_list('id', flat=True))
            if invalid_district_ids:
                errors.append('One or more selected districts do not belong to the chosen states.')

        if errors:
            for error in errors:
                messages.error(request, error)
            context = {
                'categories': categories,
                'scheme': scheme,
                'editing': True,
                'selected_state_ids': set(selected_state_ids),
                'selected_district_ids': set(selected_district_ids),
                'selected_category_ids': set(scheme.eligible_categories.values_list('id', flat=True)),
                'geographical_scope': geographical_scope,
                'application_deadline_value': deadline_raw,
                **location_context(),
            }
            return render(request, 'accounts/create_scheme.html', context)

        scheme.name = request.POST.get('name', '').strip()
        scheme.description = request.POST.get('description', '').strip()
        scheme.application_deadline = application_deadline
        scheme.subsidy_amount = request.POST.get('subsidy_amount')
        scheme.min_income = request.POST.get('min_income') or None
        scheme.max_income = request.POST.get('max_income') or None
        scheme.min_land = request.POST.get('min_land') or None
        scheme.max_land = request.POST.get('max_land') or None
        scheme.land_area_unit = request.POST.get('land_area_unit') or 'sqm'
        scheme.actual_price = request.POST.get('actual_price') or None
        scheme.geographical_scope = geographical_scope
        if request.FILES.get('scheme_image'):
            scheme.scheme_image = request.FILES['scheme_image']
        scheme.save()

        scheme.eligible_categories.set(
            Category.objects.filter(id__in=request.POST.getlist('eligible_categories'))
        )
        scheme.locations.all().delete()
        if geographical_scope == 'all_states':
            rows = [SchemeLocation(scheme=scheme, state=state, district=None) for state in State.objects.all()]
            if rows:
                SchemeLocation.objects.bulk_create(rows)
        else:
            selected_district_lookup = {}
            for district in District.objects.filter(id__in=selected_district_ids):
                selected_district_lookup.setdefault(district.state_id, []).append(district.id)

            rows = []
            for state in selected_states:
                district_ids = selected_district_lookup.get(state.id, [])
                if district_ids:
                    rows.extend(
                        SchemeLocation(scheme=scheme, state=state, district_id=district_id)
                        for district_id in sorted(district_ids)
                    )
                else:
                    rows.append(SchemeLocation(scheme=scheme, state=state, district=None))
            if rows:
                SchemeLocation.objects.bulk_create(rows)
        messages.success(request, 'Scheme details updated successfully.')
        return redirect('scheme_details', scheme_id=scheme.id)

    context = {
        'categories': categories,
        'scheme': scheme,
        'editing': True,
        'application_deadline_value': scheme.application_deadline.isoformat() if scheme.application_deadline else '',
        'geographical_scope': scheme.geographical_scope,
        'selected_state_ids': set(scheme.locations.filter(district__isnull=True).values_list('state_id', flat=True)) | set(scheme.locations.exclude(district__isnull=True).values_list('state_id', flat=True)),
        'selected_district_ids': set(scheme.locations.exclude(district__isnull=True).values_list('district_id', flat=True)),
        'selected_category_ids': set(scheme.eligible_categories.values_list('id', flat=True)),
        **location_context(),
    }
    return render(request, 'accounts/create_scheme.html', context)


@login_required
@user_passes_test(is_company)
def delete_scheme(request, scheme_id):
    scheme = get_object_or_404(Scheme, id=scheme_id, company=request.user.company)
    if request.method != 'POST':
        messages.error(request, 'Scheme deletion must be confirmed with a POST request.')
        return redirect('company_dashboard')

    scheme_name = scheme.name
    scheme.delete()
    messages.success(request, f"Scheme '{scheme_name}' was deleted successfully.")
    return redirect('company_dashboard')

@login_required
@user_passes_test(is_company)
def view_applications(request, scheme_id=None):
    company = request.user.company
    schemes = company.schemes.all()
    
    if scheme_id:
        applications = Application.objects.filter(scheme_id=scheme_id).order_by('-submitted_at')
        selected_scheme = get_object_or_404(Scheme, id=scheme_id, company=company)
    else:
        applications = Application.objects.filter(scheme__in=schemes).order_by('-submitted_at')
        selected_scheme = None
    
    if request.method == 'POST':
        application_id = request.POST.get('application_id')
        action = request.POST.get('action')
        reason = request.POST.get('reason', '')
        
        application = get_object_or_404(Application, id=application_id, scheme__company=company)
        
        if action == 'approve':
            application.status = 'approved'
            application.reviewed_at = timezone.now()
            application.save()
            add_notification(application.farmer.user, f"Your application for '{application.scheme.name}' is APPROVED!", 'success')
            messages.success(request, 'Application approved')
        elif action == 'reject':
            application.status = 'rejected'
            application.rejection_reason = reason
            application.reviewed_at = timezone.now()
            application.save()
            add_notification(application.farmer.user, f"Your application for '{application.scheme.name}' is REJECTED. Reason: {reason}", 'error')
            messages.success(request, 'Application rejected')
        elif action == 'redflag':
            if application.status == 'pending':
                application.status = 'fraud'
                application.fraud_reason = reason
                application.reviewed_at = timezone.now()
                application.save()

                farmer = application.farmer
                farmer.redflag_count += 1
                farmer.save(update_fields=['redflag_count'])

                if farmer.redflag_count >= 3:
                    admin_users = User.objects.filter(is_superuser=True)
                    for admin in admin_users:
                        add_notification(admin, f"Farmer {farmer.full_name} has received {farmer.redflag_count} red flags! Please review.", 'error')

                add_notification(application.farmer.user, f"Your application for '{application.scheme.name}' is flagged as FRAUD. Reason: {reason}", 'error')
                messages.success(request, 'Application marked as fraud/redflag')
            else:
                messages.error(request, 'Only pending applications can receive a new red flag.')

        
        if scheme_id:
            return redirect('view_applications_scheme', scheme_id=scheme_id)
        return redirect('view_applications')
    
    context = {
        'applications': applications,
        'schemes': schemes,
        'selected_scheme': selected_scheme,
    }
    return render(request, 'accounts/view_applications.html', context)

# Admin Views
@login_required
@user_passes_test(is_admin)
def admin_dashboard(request):
    pending_companies = Company.objects.filter(is_approved=False, is_rejected=False).select_related('user')
    total_farmers = Farmer.objects.count()
    total_companies = Company.objects.count()
    total_applications = Application.objects.count()
    pending_applications = Application.objects.filter(status='pending').count()

    company_search = request.GET.get('company_search', '').strip()
    scheme_search = request.GET.get('scheme_search', '').strip()
    farmer_search = request.GET.get('farmer_search', '').strip()

    farmers = Farmer.objects.select_related('user').order_by('-redflag_count', 'full_name')
    if farmer_search:
        farmers = farmers.filter(
            Q(full_name__icontains=farmer_search)
            | Q(user__email__icontains=farmer_search)
            | Q(phone__icontains=farmer_search)
        )
    
    all_schemes = Scheme.objects.select_related('company').annotate(
        avg_rating=Avg('applications__feedback__rating'),
        feedback_count=Count('applications__feedback', distinct=True),
        low_feedback_count=Count(
            'applications__feedback',
            filter=Q(applications__feedback__rating__lte=2),
            distinct=True,
        ),
    ).order_by('company__company_name', 'name')
    if scheme_search:
        all_schemes = all_schemes.filter(
            Q(name__icontains=scheme_search)
            | Q(company__company_name__icontains=scheme_search)
        )
    companies = Company.objects.select_related('user').annotate(
        average_feedback_rating=Avg('feedbacks__rating'),
        negative_feedback_count=Count('feedbacks', filter=Q(feedbacks__rating__lte=2)),
    ).order_by('company_name')
    if company_search:
        companies = companies.filter(
            Q(company_name__icontains=company_search)
            | Q(user__email__icontains=company_search)
        )
    
    context = {
        'pending_companies': pending_companies,
        'companies': companies,
        'total_farmers': total_farmers,
        'total_companies': total_companies,
        'total_applications': total_applications,
        'pending_applications': pending_applications,
        'farmers': farmers,
        'all_schemes': all_schemes,
        'company_search': company_search,
        'scheme_search': scheme_search,
        'farmer_search': farmer_search,
        'categories': Category.objects.order_by('name'),
    }
    return render(request, 'accounts/admin_dashboard.html', context)


@login_required
@user_passes_test(is_admin)
@require_POST
def manage_category(request):
    action = request.POST.get('action', '')
    category_name = request.POST.get('name', '').strip()

    if action == 'add':
        if not category_name:
            messages.error(request, 'Category name is required.')
        elif len(category_name) > 50:
            messages.error(request, 'Category name cannot exceed 50 characters.')
        elif Category.objects.filter(name__iexact=category_name).exists():
            messages.error(request, 'A category with that name already exists.')
        else:
            Category.objects.create(name=category_name)
            messages.success(request, f'Category "{category_name}" added.')
    elif action == 'edit':
        category = get_object_or_404(Category, pk=request.POST.get('category_id'))
        if not category_name:
            messages.error(request, 'Category name is required.')
        elif len(category_name) > 50:
            messages.error(request, 'Category name cannot exceed 50 characters.')
        elif Category.objects.filter(name__iexact=category_name).exclude(pk=category.pk).exists():
            messages.error(request, 'A category with that name already exists.')
        else:
            category.name = category_name
            category.save(update_fields=['name'])
            messages.success(request, f'Category updated to "{category_name}".')
    elif action == 'delete':
        category = get_object_or_404(Category, pk=request.POST.get('category_id'))
        category_name = category.name
        category.delete()
        messages.success(request, f'Category "{category_name}" deleted.')
    else:
        messages.error(request, 'Invalid category action.')

    return redirect(f"{reverse('admin_dashboard')}#category-management")

@login_required
@user_passes_test(is_admin)
def admin_farmer_reviews(request):
    reviews = Feedback.objects.select_related(
        'application__scheme__company', 'company'
    ).order_by('-created_at', '-id')

    search_query = request.GET.get('q', '').strip()
    if search_query:
        reviews = reviews.filter(
            Q(comment__icontains=search_query)
            | Q(application__scheme__name__icontains=search_query)
            | Q(application__scheme__company__company_name__icontains=search_query)
        )

    selected_company = request.GET.get('company', '').strip()
    if selected_company.isdigit() and Company.objects.filter(id=int(selected_company)).exists():
        reviews = reviews.filter(application__scheme__company_id=int(selected_company))
    else:
        selected_company = ''

    selected_scheme = request.GET.get('scheme', '').strip()
    if selected_scheme.isdigit() and Scheme.objects.filter(id=int(selected_scheme)).exists():
        reviews = reviews.filter(application__scheme_id=int(selected_scheme))
    else:
        selected_scheme = ''

    selected_rating = request.GET.get('rating', '').strip()
    if selected_rating in {'1', '2', '3', '4', '5'}:
        reviews = reviews.filter(rating=int(selected_rating))
    else:
        selected_rating = ''

    all_feedback = Feedback.objects.all()
    summary = all_feedback.aggregate(average=Avg('rating'), total=Count('id'))
    company_count = all_feedback.values('application__scheme__company_id').distinct().count()
    scheme_count = all_feedback.values('application__scheme_id').distinct().count()
    rating_counts = dict(
        all_feedback.values('rating').annotate(total=Count('id')).values_list('rating', 'total')
    )
    rating_distribution = [
        {
            'rating': rating,
            'stars': '\u2605' * rating + '\u2606' * (5 - rating),
            'count': rating_counts.get(rating, 0),
        }
        for rating in range(5, 0, -1)
    ]

    paginator = Paginator(reviews, 9)
    page_obj = paginator.get_page(request.GET.get('page'))
    review_cards = [
        {
            'feedback': feedback,
            'stars': '\u2605' * feedback.rating + '\u2606' * (5 - feedback.rating),
        }
        for feedback in page_obj.object_list
    ]
    page_numbers = range(
        max(1, page_obj.number - 2),
        min(paginator.num_pages, page_obj.number + 2) + 1,
    ) if paginator.num_pages else []
    pagination_params = request.GET.copy()
    pagination_params.pop('page', None)

    return render(request, 'accounts/admin_farmer_reviews.html', {
        'companies': Company.objects.order_by('company_name'),
        'schemes': Scheme.objects.select_related('company').order_by('name'),
        'review_cards': review_cards,
        'page_obj': page_obj,
        'page_numbers': page_numbers,
        'pagination_query': pagination_params.urlencode(),
        'overall_rating': summary['average'],
        'review_count': summary['total'],
        'companies_reviewed': company_count,
        'schemes_reviewed': scheme_count,
        'rating_distribution': rating_distribution,
        'search_query': search_query,
        'selected_company': selected_company,
        'selected_scheme': selected_scheme,
        'selected_rating': selected_rating,
    })

@login_required
@user_passes_test(is_admin)
@require_POST
def approve_company(request, company_id):
    company = get_object_or_404(Company, id=company_id)
    if company.is_rejected:
        messages.error(request, 'Rejected companies cannot be approved.')
        return redirect('admin_dashboard')
    if company.is_approved:
        messages.info(request, f"Company '{company.company_name}' is already approved.")
        return redirect('admin_dashboard')
    if not all((
        company.registration_document,
        company.gst_registration_certificate,
        company.address_proof,
    )):
        messages.error(request, 'Cannot approve this company until all three verification documents are provided.')
        return redirect('admin_dashboard')

    company.is_approved = True
    company.save()
    add_notification(company.user, f"Your company '{company.company_name}' has been APPROVED! You can now create schemes.", 'success')
    messages.success(request, f'Company {company.company_name} approved')
    return redirect('admin_dashboard')

@login_required
@user_passes_test(is_admin)
@require_POST
def reject_company(request, company_id):
    company = get_object_or_404(Company.objects.select_related('user'), id=company_id)
    if company.is_approved or company.is_rejected:
        messages.error(request, 'Only pending companies can be rejected.')
        return redirect('admin_dashboard')

    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, 'Please provide a reason before rejecting this company.')
        return redirect('admin_dashboard')

    company.is_rejected = True
    company.rejection_reason = reason
    company.save(update_fields=['is_rejected', 'rejection_reason'])
    company.user.is_active = False
    company.user.save(update_fields=['is_active'])
    add_notification(
        company.user,
        f"Your company '{company.company_name}' registration was rejected. Reason: {reason}",
        'error',
    )
    messages.success(request, f"Company '{company.company_name}' was rejected; its registration details are reserved.")
    return redirect('admin_dashboard')

@login_required
@user_passes_test(is_admin)
@require_POST
def block_company(request, company_id):
    company = get_object_or_404(Company.objects.select_related('user'), id=company_id)
    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, 'Please provide a reason before blocking this company.')
        return redirect('admin_dashboard')

    company.user.is_active = False
    company.user.save(update_fields=['is_active'])
    company.schemes.update(is_active=False)
    add_notification(
        company.user,
        f"Your company '{company.company_name}' has been blocked. Reason: {reason}",
        'error',
    )
    messages.success(request, f"Company '{company.company_name}' has been blocked.")
    return redirect('admin_dashboard')

@login_required
@user_passes_test(is_admin)
def remove_company(request, company_id):
    company = get_object_or_404(Company, id=company_id)
    applications = Application.objects.filter(scheme__company=company, status='pending')
    for app in applications:
        add_notification(app.farmer.user, f"Company '{company.company_name}' has been removed. Your application is cancelled.", 'warning')
    company.delete()
    messages.success(request, 'Company removed successfully')
    return redirect('admin_dashboard')

@login_required
@user_passes_test(is_admin)
def remove_farmer(request, farmer_id):
    farmer = get_object_or_404(Farmer, id=farmer_id)
    user = farmer.user
    farmer.delete()
    user.delete()
    messages.success(request, 'Farmer removed successfully')
    return redirect('admin_dashboard')

@login_required
@user_passes_test(is_admin)
@require_POST
def block_scheme(request, scheme_id):
    scheme = get_object_or_404(Scheme.objects.select_related('company__user'), id=scheme_id)
    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, 'Please provide a reason before blocking this scheme.')
        return redirect('admin_dashboard')
    if not Feedback.objects.filter(application__scheme=scheme, rating__lte=2).exists():
        messages.error(request, 'Only schemes with low-rated feedback can be blocked from this action.')
        return redirect('admin_dashboard')

    scheme.is_active = False
    scheme.save(update_fields=['is_active'])
    add_notification(
        scheme.company.user,
        f"Your scheme '{scheme.name}' has been blocked due to low feedback. Reason: {reason}",
        'error',
        scheme=scheme,
    )
    messages.success(request, f"Scheme '{scheme.name}' has been blocked.")
    return redirect('admin_dashboard')

@login_required
@user_passes_test(is_admin)
@require_POST
def block_farmer(request, farmer_id):
    farmer = get_object_or_404(Farmer.objects.select_related('user'), id=farmer_id)
    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, 'Please provide a reason before blocking this farmer.')
        return redirect('admin_dashboard')

    user = farmer.user
    user.is_active = False
    user.save(update_fields=['is_active'])
    add_notification(
        user,
        f"Your farmer account has been blocked. Reason: {reason}",
        'error',
    )
    messages.success(request, f"Farmer '{farmer.full_name}' has been blocked. Reason: {reason}")
    return redirect('admin_dashboard')

@login_required
@user_passes_test(is_farmer)
def edit_farmer_profile(request):
    farmer = request.user.farmer
    if request.method == 'POST':
        errors = []
        full_name = request.POST.get('full_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        aadhaar = request.POST.get('aadhaar', farmer.aadhaar).strip()
        address = request.POST.get('address', '').strip()
        state_id = request.POST.get('state', '').strip()
        district_id = request.POST.get('district', '').strip()
        income = request.POST.get('income', '').strip()
        land_area_value = request.POST.get('land_area_value', '').strip()
        land_area_unit = request.POST.get('land_area_unit', '').strip()
        selected_categories = request.POST.getlist('categories')

        if not full_name:
            errors.append("Full name is required.")
        if len(phone) != 10 or not phone.isdigit():
            errors.append("Phone number must be exactly 10 digits.")
        if phone != farmer.phone and Farmer.objects.filter(phone=phone).exists():
            errors.append("Phone number already registered.")
        if len(aadhaar) != 12 or not aadhaar.isdigit():
            errors.append("Aadhaar must be exactly 12 digits.")
        if aadhaar != farmer.aadhaar and Farmer.objects.filter(aadhaar=aadhaar).exists():
            errors.append("Aadhaar number already registered.")
        if len(address) < 10:
            errors.append("Please enter complete address (at least 10 characters).")
        state = State.objects.filter(id=state_id).first() if state_id.isdigit() else None
        district = District.objects.filter(id=district_id, state=state).first() if district_id.isdigit() and state else None
        if not state:
            errors.append("Please select a valid state.")
        if not district:
            errors.append("Please select a district belonging to the selected state.")
        try:
            if float(income) < 0:
                errors.append("Income cannot be negative.")
        except (ValueError, TypeError):
            errors.append("Please enter a valid income.")
        try:
            area_value = Decimal(land_area_value)
            if area_value <= 0:
                errors.append("Land area must be greater than zero.")
            if land_area_unit not in dict(LAND_AREA_UNITS):
                errors.append("Please select a valid land area unit.")
        except (InvalidOperation, TypeError):
            errors.append("Please enter a valid land area.")
        if not selected_categories:
            errors.append("Please select at least one farming category.")

        category_records = Category.objects.filter(name__in=selected_categories)
        if len(set(selected_categories)) != category_records.count():
            errors.append("Please select valid farming categories.")

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            farmer.full_name = full_name
            farmer.phone = phone
            farmer.aadhaar = aadhaar
            farmer.address = address
            farmer.state = state
            farmer.district = district
            farmer.income = income
            area_sqm = land_area_to_sqm(area_value, land_area_unit)
            farmer.land_size = (area_sqm / Decimal('4046.8564224')).quantize(Decimal('0.01'))
            farmer.land_area_value = area_value
            farmer.land_area_unit = land_area_unit
            farmer.land_area_sqm = area_sqm
            farmer.save()

            # Update user first_name
            request.user.first_name = full_name
            request.user.save()

            farmer.categories.set(category_records)

            add_notification(request.user, "Your profile details have been updated successfully.", 'success')
            messages.success(request, "Profile updated successfully!")
            return redirect('farmer_dashboard')

    context = {
        'farmer': farmer,
        'categories': Category.objects.order_by('name'),
        'selected_categories': [c.name for c in farmer.categories.all()],
        'selected_state': str(farmer.state_id or ''),
        'selected_district': str(farmer.district_id or ''),
        **location_context(),
    }
    return render(request, 'accounts/edit_farmer_profile.html', context)


@login_required
@user_passes_test(is_company)
def edit_company_profile(request):
    company = request.user.company
    if request.method == 'POST':
        errors = []
        company_name = request.POST.get('company_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        gst_number = request.POST.get('gst_number', '').strip().upper()

        if not company_name:
            errors.append("Company name is required.")
        elif len(company_name) > 200:
            errors.append("Company name cannot exceed 200 characters.")
        elif Company.objects.filter(company_name__iexact=company_name).exclude(pk=company.pk).exists():
            errors.append("Company name already registered.")
        if len(phone) != 10 or not phone.isdigit():
            errors.append("Phone number must be exactly 10 digits.")
        elif Company.objects.filter(phone=phone).exclude(pk=company.pk).exists():
            errors.append("Phone number already registered.")
        if not gst_number:
            errors.append("GST number is required.")
        elif len(gst_number) > 15:
            errors.append("GST number cannot exceed 15 characters.")
        if gst_number != company.gst_number and Company.objects.filter(gst_number__iexact=gst_number).exclude(pk=company.pk).exists():
            errors.append("GST number already registered.")

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            company.company_name = company_name
            company.phone = phone
            company.gst_number = gst_number

            if 'registration_document' in request.FILES:
                doc = request.FILES['registration_document']
                from django.core.files.base import ContentFile
                doc_path = default_storage.save(
                    f'company_docs/{company.id}/{doc.name}',
                    ContentFile(doc.read())
                )
                company.registration_document = doc_path

            company.save()
            add_notification(request.user, "Your company profile has been updated successfully.", 'success')
            messages.success(request, "Company profile updated successfully!")
            return redirect('company_dashboard')

    context = {'company': company}
    return render(request, 'accounts/edit_company_profile.html', context)


# Notification Views
@login_required
def notifications(request):
    notifs = request.user.notifications.all()
    
    if request.method == 'POST':
        request.user.notifications.update(is_read=True)
        return redirect('notifications')
    
    context = {'notifications': notifs}
    return render(request, 'accounts/notifications.html', context)

@login_required
def mark_notification_read(request, notification_id):
    notification = get_object_or_404(Notification, id=notification_id, user=request.user)
    notification.is_read = True
    notification.save()
    return redirect('notifications')
