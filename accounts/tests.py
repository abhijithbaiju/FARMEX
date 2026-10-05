from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from .models import (
    Company, Farmer, Scheme, Application, Feedback, Category, State, District,
    SchemeLocation, land_area_to_sqm,
)
from .views import notify_eligible_farmers, scheme_matches_farmer
from .location_data import LOCATION_DATA


class PasswordValidationPolicyTests(TestCase):
    def test_password_may_resemble_user_but_other_strength_rules_remain(self):
        user = User(username='maria@example.com', email='maria@example.com', first_name='Maria Farmer')

        validate_password('Maria Farmer!2026', user=user)

        for weak_password in ('abc', '123456789012', 'password'):
            with self.subTest(password=weak_password):
                with self.assertRaises(ValidationError):
                    validate_password(weak_password, user=user)


class CompanyLoginTests(TestCase):
    def test_pending_company_login_does_not_open_pending_dashboard(self):
        company_user = User.objects.create_user(
            username='pending-company@example.com',
            email='pending-company@example.com',
            password='securepass123',
        )
        Company.objects.create(
            user=company_user,
            company_name='Pending Company',
            gst_number='123456789012345',
        )

        response = self.client.post(
            reverse('login'),
            {'username': 'pending-company@example.com', 'password': 'securepass123'},
        )

        self.assertRedirects(response, reverse('login'))
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        dashboard_response = self.client.get(reverse('company_dashboard'))
        self.assertRedirects(dashboard_response, f"{reverse('login')}?next={reverse('company_dashboard')}")


class AdminCompanyManagementTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='company-admin',
            email='admin@example.com',
            password='securepass123',
        )
        self.company_user = User.objects.create_user(
            username='reviewed-company',
            email='company@example.com',
            password='securepass123',
        )
        self.company = Company.objects.create(
            user=self.company_user,
            company_name='Reviewed Company',
            gst_number='123456789012345',
            is_approved=True,
        )
        self.scheme = Scheme.objects.create(
            company=self.company,
            name='Company Offer',
            description='Active company offer',
            subsidy_amount=1000,
        )

    def test_total_companies_link_displays_company_and_block_form(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertContains(response, 'href="#company-list"')
        self.assertContains(response, 'Manage Companies')
        self.assertContains(response, 'href="#pending-companies"')
        self.assertContains(response, 'Company Verification')
        self.assertContains(response, 'href="#farmer-list"')
        self.assertContains(response, 'Manage Users')
        self.assertContains(response, 'href="#scheme-review"')
        self.assertContains(response, 'Manage Schemes')
        self.assertContains(response, 'View pending verifications')
        self.assertContains(response, self.company.company_name)
        self.assertContains(response, 'name="reason"')
        self.assertContains(response, 'Low Ratings')

    def test_admin_dashboard_section_searches_filter_company_scheme_and_farmer_querysets(self):
        other_company_user = User.objects.create_user(username='searchable-company', password='securepass123')
        other_company = Company.objects.create(
            user=other_company_user,
            company_name='Searchable Agro Company',
            gst_number='923456789012345',
        )
        other_scheme = Scheme.objects.create(
            company=other_company,
            name='Searchable Seed Scheme',
            description='Searchable scheme',
            subsidy_amount=1200,
        )
        farmer_user = User.objects.create_user(
            username='searchable-farmer',
            email='searchable-farmer@example.com',
            password='securepass123',
        )
        Farmer.objects.create(
            user=farmer_user,
            full_name='Searchable Farmer Name',
            phone='9765400100',
            aadhaar='2345600100',
            address='Private Searchable Address',
            income=25000,
            land_size=2,
        )
        self.client.force_login(self.admin_user)

        company_response = self.client.get(reverse('admin_dashboard'), {'company_search': 'Searchable Agro'})
        self.assertEqual(list(company_response.context['companies']), [other_company])
        self.assertContains(company_response, 'name="company_search"')
        self.assertContains(company_response, 'Searchable Agro Company')

        scheme_response = self.client.get(reverse('admin_dashboard'), {'scheme_search': 'Searchable Seed'})
        self.assertEqual(list(scheme_response.context['all_schemes']), [other_scheme])
        self.assertContains(scheme_response, 'name="scheme_search"')

        farmer_response = self.client.get(reverse('admin_dashboard'), {'farmer_search': 'Searchable Farmer'})
        self.assertEqual(len(farmer_response.context['farmers']), 1)
        self.assertEqual(farmer_response.context['farmers'][0].full_name, 'Searchable Farmer Name')
        self.assertContains(farmer_response, 'name="farmer_search"')

    def test_company_review_summarizes_low_feedback_ratings(self):
        farmer_user = User.objects.create_user(username='feedback-farmer', password='securepass123')
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name='Feedback Farmer',
            phone='9876543210',
            aadhaar='123456789012',
            address='Farm address',
            income=20000,
            land_size=2,
        )
        application = Application.objects.create(farmer=farmer, scheme=self.scheme)
        Feedback.objects.create(
            farmer=farmer,
            company=self.company,
            application=application,
            rating=2,
            comment='Poor service',
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_dashboard'))

        company_row = response.context['companies'].get(pk=self.company.pk)
        self.assertEqual(company_row.negative_feedback_count, 1)
        self.assertEqual(float(company_row.average_feedback_rating), 2.0)
        self.assertContains(response, '2.0 / 5')

    def test_admin_can_block_company_with_reason(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse('block_company', args=[self.company.id]),
            {'reason': 'Multiple reports of fraudulent activity.'},
        )

        self.assertRedirects(response, reverse('admin_dashboard'))
        self.company_user.refresh_from_db()
        self.scheme.refresh_from_db()
        self.assertFalse(self.company_user.is_active)
        self.assertFalse(self.scheme.is_active)
        self.assertTrue(
            self.company_user.notifications.filter(
                message__contains='Multiple reports of fraudulent activity.'
            ).exists()
        )

    def test_company_cannot_be_blocked_without_reason(self):
        self.client.force_login(self.admin_user)

        self.client.post(reverse('block_company', args=[self.company.id]), {'reason': '  '})

        self.company_user.refresh_from_db()
        self.assertTrue(self.company_user.is_active)
        self.assertFalse(self.company_user.notifications.exists())

    def test_registration_requires_and_saves_company_phone_number(self):
        registration_data = {
            'company_name': 'Phone Company',
            'company_registration_number': 'REG-PHONE-001',
            'gst_number': '123456789012346',
            'country': 'India',
            'state': 'Kerala',
            'address': '12 Farm Road, Kochi',
            'pincode': '682001',
            'email': 'phone-company@example.com',
            'password': 'securepass123',
            'confirm_password': 'securepass123',
            'registration_document': SimpleUploadedFile('registration.pdf', b'registration'),
            'gst_registration_certificate': SimpleUploadedFile('gst.pdf', b'gst certificate'),
            'address_proof': SimpleUploadedFile('address.pdf', b'address proof'),
        }

        response = self.client.post(reverse('company_register'), registration_data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='phone-company@example.com').exists())

        registration_data['phone'] = '9876543210'
        registration_data['registration_document'] = SimpleUploadedFile('registration.pdf', b'registration')
        registration_data['gst_registration_certificate'] = SimpleUploadedFile('gst.pdf', b'gst certificate')
        registration_data['address_proof'] = SimpleUploadedFile('address.pdf', b'address proof')
        response = self.client.post(reverse('company_register'), registration_data)

        self.assertRedirects(response, reverse('login'))
        company = Company.objects.get(user__email='phone-company@example.com')
        self.assertEqual(company.phone, '9876543210')

    def test_rejected_company_identifiers_cannot_be_reused(self):
        self.company.is_approved = False
        self.company.company_name = 'Rejected Company'
        self.company.phone = '9876543210'
        self.company.company_registration_number = 'REG-REJECT-2026'
        self.company.gst_number = '523456789012345'
        self.company.save()
        self.client.force_login(self.admin_user)

        rejection_response = self.client.post(
            reverse('reject_company', args=[self.company.id]),
            {'reason': 'Verification documents were fraudulent.'},
        )

        self.assertRedirects(rejection_response, reverse('admin_dashboard'))
        self.company.refresh_from_db()
        self.company_user.refresh_from_db()
        self.assertTrue(self.company.is_rejected)
        self.assertEqual(self.company.rejection_reason, 'Verification documents were fraudulent.')
        self.assertFalse(self.company_user.is_active)
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())
        self.assertFalse(self.client.get(reverse('admin_dashboard')).context['pending_companies'].filter(pk=self.company.pk).exists())

        self.client.logout()
        reused_identity_values = {
            'company_name': self.company.company_name,
            'phone': self.company.phone,
            'company_registration_number': self.company.company_registration_number,
            'gst_number': self.company.gst_number,
            'email': self.company_user.email,
        }
        for identity_field, duplicate_value in reused_identity_values.items():
            registration_data = {
                'company_name': 'New Applicant Company',
                'phone': '9876500000',
                'company_registration_number': 'REG-NEW-2026',
                'gst_number': '623456789012345',
                'country': 'India',
                'state': 'Kerala',
                'address': '12 Another Farm Road, Kochi',
                'pincode': '682002',
                'email': 'new-applicant@example.com',
                'password': 'SecurePass2026!A',
                'confirm_password': 'SecurePass2026!A',
                'registration_document': SimpleUploadedFile('registration.pdf', b'registration'),
                'gst_registration_certificate': SimpleUploadedFile('gst.pdf', b'gst certificate'),
                'address_proof': SimpleUploadedFile('address.pdf', b'address proof'),
            }
            registration_data[identity_field] = duplicate_value

            with self.subTest(identity_field=identity_field):
                response = self.client.post(reverse('company_register'), registration_data)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(User.objects.filter(username='new-applicant@example.com').exists())

    def test_company_registration_validates_email_and_configured_password_rules(self):
        registration_data = {
            'company_name': 'Validated Applicant Company',
            'phone': '9876500001',
            'company_registration_number': 'REG-VALID-2026',
            'gst_number': '723456789012345',
            'country': 'India',
            'state': 'Kerala',
            'address': '15 Another Farm Road, Kochi',
            'pincode': '682003',
            'email': 'not-an-email',
            'password': '123',
            'confirm_password': '123',
            'registration_document': SimpleUploadedFile('registration.pdf', b'registration'),
            'gst_registration_certificate': SimpleUploadedFile('gst.pdf', b'gst certificate'),
            'address_proof': SimpleUploadedFile('address.pdf', b'address proof'),
        }

        response = self.client.post(reverse('company_register'), registration_data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='not-an-email').exists())
        self.assertFalse(Company.objects.filter(company_name='Validated Applicant Company').exists())

    def test_company_profile_cannot_reuse_another_company_identity(self):
        other_user = User.objects.create_user(username='other-profile-company', password='securepass123')
        other_company = Company.objects.create(
            user=other_user,
            company_name='Other Profile Company',
            phone='9876543211',
            gst_number='823456789012345',
        )
        self.company.company_name = 'Original Profile Company'
        self.company.phone = '9876543210'
        self.company.gst_number = '723456789012345'
        self.company.save()
        self.client.force_login(self.company_user)

        response = self.client.post(
            reverse('edit_company_profile'),
            {
                'company_name': other_company.company_name,
                'phone': other_company.phone,
                'gst_number': other_company.gst_number,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.company.refresh_from_db()
        self.assertEqual(self.company.company_name, 'Original Profile Company')
        self.assertEqual(self.company.phone, '9876543210')
        self.assertEqual(self.company.gst_number, '723456789012345')

    def test_pending_company_review_shows_all_details_and_document_links(self):
        self.company.is_approved = False
        self.company.company_registration_number = 'REG-REVIEW-2026'
        self.company.country = 'India'
        self.company.state = 'Kerala'
        self.company.address = '12 Farm Road, Kochi'
        self.company.pincode = '682001'
        self.company.registration_document = 'company_docs/review-registration.pdf'
        self.company.gst_registration_certificate = 'company_docs/review-gst.pdf'
        self.company.address_proof = 'company_docs/review-address.pdf'
        self.company.save()
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertContains(response, 'Review details and documents')
        self.assertContains(response, 'REG-REVIEW-2026')
        self.assertContains(response, '12 Farm Road, Kochi')
        self.assertContains(response, self.company_user.email)
        self.assertContains(response, 'Registration certificate')
        self.assertContains(response, 'GST registration certificate')
        self.assertContains(response, 'Company address proof')
        self.assertContains(response, 'review-registration.pdf')
        self.assertContains(response, 'review-gst.pdf')
        self.assertContains(response, 'review-address.pdf')

    def test_company_approval_and_rejection_require_post(self):
        self.company.is_approved = False
        self.company.save(update_fields=['is_approved'])
        self.client.force_login(self.admin_user)

        approve_get = self.client.get(reverse('approve_company', args=[self.company.id]))
        reject_get = self.client.get(reverse('reject_company', args=[self.company.id]))

        self.assertEqual(approve_get.status_code, 405)
        self.assertEqual(reject_get.status_code, 405)
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())

    def test_company_rejection_requires_reason(self):
        self.company.is_approved = False
        self.company.save(update_fields=['is_approved'])
        self.client.force_login(self.admin_user)

        self.client.post(reverse('reject_company', args=[self.company.id]), {'reason': '  '})

        self.company.refresh_from_db()
        self.company_user.refresh_from_db()
        self.assertFalse(self.company.is_rejected)
        self.assertTrue(self.company_user.is_active)

    def test_company_cannot_be_approved_until_all_documents_are_present(self):
        self.company.is_approved = False
        self.company.save(update_fields=['is_approved'])
        self.client.force_login(self.admin_user)

        self.client.post(reverse('approve_company', args=[self.company.id]))

        self.company.refresh_from_db()
        self.assertFalse(self.company.is_approved)

    def test_company_can_be_approved_after_documents_are_reviewable(self):
        self.company.is_approved = False
        self.company.registration_document = 'company_docs/review-registration.pdf'
        self.company.gst_registration_certificate = 'company_docs/review-gst.pdf'
        self.company.address_proof = 'company_docs/review-address.pdf'
        self.company.save()
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse('approve_company', args=[self.company.id]))

        self.assertRedirects(response, reverse('admin_dashboard'))
        self.company.refresh_from_db()
        self.assertTrue(self.company.is_approved)


class CompanyFarmerReviewsTests(TestCase):
    def setUp(self):
        company_user = User.objects.create_user(username='reviews-company-a', password='securepass123')
        self.company = Company.objects.create(
            user=company_user,
            company_name='Reviews Company A',
            gst_number='183456789012345',
            is_approved=True,
        )
        self.scheme = Scheme.objects.create(
            company=self.company,
            name='Company A Soil Scheme',
            description='Company A soil support',
            subsidy_amount=1000,
        )
        other_company_user = User.objects.create_user(username='reviews-company-b', password='securepass123')
        self.other_company = Company.objects.create(
            user=other_company_user,
            company_name='Reviews Company B',
            gst_number='283456789012345',
            is_approved=True,
        )
        self.other_scheme = Scheme.objects.create(
            company=self.other_company,
            name='Company B Seed Scheme',
            description='Company B seed support',
            subsidy_amount=2000,
        )
        self.category = Category.objects.create(name='Review Test Crops')
        self.scheme.eligible_categories.add(self.category)
        self.other_scheme.eligible_categories.add(self.category)

    def _create_company_review(self, company, scheme, index, rating, comment):
        farmer_user = User.objects.create_user(
            username=f'company-review-farmer-{index}',
            email=f'company-review-farmer-{index}@example.com',
            password='securepass123',
        )
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name=f'Sensitive Farmer Name {index}',
            phone=f'97654{index:05d}',
            aadhaar=f'23456{index:07d}',
            address=f'Sensitive Farm Address {index}',
            income=25000,
            land_size=2,
        )
        application = Application.objects.create(farmer=farmer, scheme=scheme, status='approved')
        return Feedback.objects.create(
            farmer=farmer,
            company=company,
            application=application,
            rating=rating,
            comment=comment,
        )

    def test_reviews_are_scoped_to_authenticated_company_and_hide_farmer_identity(self):
        self._create_company_review(self.company, self.scheme, 1, 5, 'Company A private review text.')
        self._create_company_review(self.other_company, self.other_scheme, 2, 1, 'Company B private review text.')
        self.client.force_login(self.company.user)

        response = self.client.get(reverse('company_farmer_reviews'))

        self.assertContains(response, 'Company A private review text.')
        self.assertNotContains(response, 'Company B private review text.')
        self.assertNotContains(response, self.other_scheme.name)
        self.assertNotContains(response, 'Sensitive Farmer Name')
        self.assertNotContains(response, '97654')
        self.assertNotContains(response, '23456')
        self.assertEqual(response.context['review_count'], 1)
        self.assertEqual(float(response.context['overall_rating']), 5.0)

        self.client.force_login(self.other_company.user)
        other_company_response = self.client.get(reverse('company_farmer_reviews'))
        self.assertContains(other_company_response, 'Company B private review text.')
        self.assertNotContains(other_company_response, 'Company A private review text.')
        self.assertNotContains(other_company_response, self.scheme.name)

    def test_forged_other_company_scheme_filter_cannot_reveal_reviews(self):
        self._create_company_review(self.company, self.scheme, 1, 4, 'Visible Company A review.')
        self._create_company_review(self.other_company, self.other_scheme, 2, 5, 'Private Company B review.')
        self.client.force_login(self.company.user)

        response = self.client.get(
            reverse('company_farmer_reviews'),
            {'scheme': self.other_scheme.id, 'rating': '5'},
        )

        self.assertNotContains(response, 'Private Company B review.')
        self.assertNotContains(response, self.other_scheme.name)
        self.assertEqual(response.context['selected_scheme'], '')
        self.assertEqual(response.context['review_count'], 1)

    def test_scheme_and_rating_filters_are_company_limited(self):
        another_scheme = Scheme.objects.create(
            company=self.company,
            name='Company A Compost Scheme',
            description='Company A compost support',
            subsidy_amount=1200,
        )
        another_scheme.eligible_categories.add(self.category)
        self._create_company_review(self.company, self.scheme, 1, 5, 'Five star soil review.')
        self._create_company_review(self.company, self.scheme, 2, 2, 'Two star soil review.')
        self._create_company_review(self.company, another_scheme, 3, 5, 'Five star compost review.')
        self._create_company_review(self.other_company, self.other_scheme, 4, 5, 'Other company review.')
        self.client.force_login(self.company.user)

        response = self.client.get(
            reverse('company_farmer_reviews'),
            {'scheme': self.scheme.id, 'rating': '5'},
        )

        self.assertContains(response, 'Five star soil review.')
        self.assertNotContains(response, 'Two star soil review.')
        self.assertNotContains(response, 'Five star compost review.')
        self.assertNotContains(response, 'Other company review.')
        self.assertNotContains(response, self.other_scheme.name)
        self.assertContains(response, another_scheme.name)

    def test_company_aggregates_distribution_and_paginates_its_own_reviews(self):
        for index in range(1, 11):
            self._create_company_review(self.company, self.scheme, index, 5 if index <= 8 else 4, f'Company A review {index}.')
        self._create_company_review(self.other_company, self.other_scheme, 11, 1, 'Other company rating.')
        self.client.force_login(self.company.user)

        response = self.client.get(reverse('company_farmer_reviews'))

        self.assertEqual(response.context['review_count'], 10)
        self.assertEqual(float(response.context['overall_rating']), 4.8)
        self.assertEqual(response.context['rating_distribution'][0]['count'], 8)
        self.assertEqual(response.context['rating_distribution'][1]['count'], 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 10)
        self.assertEqual(len(response.context['review_cards']), 9)
        self.assertContains(response, 'Next')

        second_page = self.client.get(reverse('company_farmer_reviews'), {'page': 2})
        self.assertEqual(second_page.context['page_obj'].number, 2)
        self.assertEqual(len(second_page.context['review_cards']), 1)
        self.assertNotContains(second_page, 'Other company rating.')

    def test_empty_state_and_dashboard_summary(self):
        self.client.force_login(self.company.user)

        reviews_response = self.client.get(reverse('company_farmer_reviews'))
        dashboard_response = self.client.get(reverse('company_dashboard'))

        self.assertContains(reviews_response, 'No farmer reviews yet')
        self.assertIsNone(reviews_response.context['overall_rating'])
        self.assertEqual(reviews_response.context['review_count'], 0)
        self.assertContains(dashboard_response, 'Farmer Rating')
        self.assertContains(dashboard_response, 'View Farmer Reviews')
        self.assertContains(dashboard_response, reverse('company_farmer_reviews'))

    def test_farmer_cannot_access_company_reviews(self):
        farmer_user = User.objects.create_user(username='not-a-company', password='securepass123')
        self.client.force_login(farmer_user)

        response = self.client.get(reverse('company_farmer_reviews'))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

    def test_new_farmer_feedback_appears_for_owning_company(self):
        farmer_user = User.objects.create_user(username='new-review-farmer', password='securepass123')
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name='New Review Farmer',
            phone='9765400020',
            aadhaar='2345600020',
            address='New review farmer address',
            income=22000,
            land_size=2,
        )
        application = Application.objects.create(
            farmer=farmer,
            scheme=self.scheme,
            status='approved',
        )
        self.client.force_login(farmer_user)

        submission_response = self.client.post(
            reverse('give_feedback', args=[application.id]),
            {'rating': '4', 'comment': 'Fresh feedback from the existing form.'},
        )

        self.assertRedirects(submission_response, reverse('farmer_dashboard'))
        self.client.force_login(self.company.user)
        reviews_response = self.client.get(reverse('company_farmer_reviews'))
        self.assertContains(reviews_response, 'Fresh feedback from the existing form.')
        self.assertEqual(reviews_response.context['review_count'], 1)


class AdminFarmerReviewsTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='admin-reviews-user',
            email='admin-reviews@example.com',
            password='securepass123',
        )
        self.companies = []
        self.schemes = []
        for index, name in enumerate(('Admin Reviews Company A', 'Admin Reviews Company B'), start=1):
            user = User.objects.create_user(username=f'admin-reviews-company-{index}', password='securepass123')
            company = Company.objects.create(user=user, company_name=name, gst_number=f'8{index}3456789012345')
            scheme = Scheme.objects.create(
                company=company,
                name=f'Admin Reviews Scheme {index}',
                description='Review test scheme',
                subsidy_amount=1000,
            )
            self.companies.append(company)
            self.schemes.append(scheme)

    def _create_review(self, index, scheme, rating, comment):
        farmer_user = User.objects.create_user(
            username=f'admin-review-farmer-{index}',
            email=f'private-review-{index}@example.com',
            password='securepass123',
        )
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name=f'Private Farmer Name {index}',
            phone=f'97654{index:05d}',
            aadhaar=f'23456{index:07d}',
            address=f'Private Farmer Address {index}',
            income=25000,
            land_size=2,
        )
        application = Application.objects.create(farmer=farmer, scheme=scheme, status='approved')
        return Feedback.objects.create(
            farmer=farmer,
            company=scheme.company,
            application=application,
            rating=rating,
            comment=comment,
        )

    def test_admin_sees_all_companies_reviews_and_aggregates_without_farmer_identity(self):
        self._create_review(1, self.schemes[0], 5, 'Company A review text.')
        self._create_review(2, self.schemes[1], 3, 'Company B review text.')
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_farmer_reviews'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Company A review text.')
        self.assertContains(response, 'Company B review text.')
        self.assertContains(response, self.companies[0].company_name)
        self.assertContains(response, self.companies[1].company_name)
        self.assertNotContains(response, 'Private Farmer Name')
        self.assertNotContains(response, '97654')
        self.assertNotContains(response, '23456')
        self.assertNotContains(response, 'Private Farmer Address')
        self.assertNotContains(response, '@example.com')
        self.assertEqual(response.context['review_count'], 2)
        self.assertEqual(float(response.context['overall_rating']), 4.0)
        self.assertEqual(response.context['companies_reviewed'], 2)
        self.assertEqual(response.context['schemes_reviewed'], 2)

        dashboard_response = self.client.get(reverse('admin_dashboard'))
        self.assertContains(dashboard_response, reverse('admin_farmer_reviews'))

    def test_empty_state_has_no_synthetic_rating_or_reviews(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_farmer_reviews'))

        self.assertContains(response, 'No farmer reviews yet')
        self.assertIsNone(response.context['overall_rating'])
        self.assertEqual(response.context['review_count'], 0)

    def test_database_filters_search_company_scheme_and_rating(self):
        self._create_review(1, self.schemes[0], 5, 'Excellent soil review.')
        self._create_review(2, self.schemes[0], 2, 'Needs improvement.')
        self._create_review(3, self.schemes[1], 5, 'Excellent seed review.')
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_farmer_reviews'), {
            'q': 'Excellent',
            'company': self.companies[0].id,
            'scheme': self.schemes[0].id,
            'rating': '5',
        })

        self.assertContains(response, 'Excellent soil review.')
        self.assertNotContains(response, 'Needs improvement.')
        self.assertNotContains(response, 'Excellent seed review.')
        self.assertEqual(response.context['page_obj'].paginator.count, 1)

        company_search = self.client.get(reverse('admin_farmer_reviews'), {'q': 'Company B'})
        self.assertContains(company_search, 'Excellent seed review.')
        self.assertNotContains(company_search, 'Excellent soil review.')

        scheme_search = self.client.get(reverse('admin_farmer_reviews'), {'q': self.schemes[0].name})
        self.assertContains(scheme_search, 'Excellent soil review.')
        self.assertNotContains(scheme_search, 'Excellent seed review.')

    def test_pagination_keeps_filters_and_non_admins_are_denied(self):
        for index in range(1, 11):
            self._create_review(index, self.schemes[0], 5, f'Paginated review {index}.')
        self.client.force_login(self.admin_user)

        first_page = self.client.get(reverse('admin_farmer_reviews'), {'company': self.companies[0].id})
        self.assertEqual(len(first_page.context['review_cards']), 9)
        self.assertContains(first_page, 'Next')
        second_page = self.client.get(reverse('admin_farmer_reviews'), {
            'company': self.companies[0].id,
            'page': 2,
        })
        self.assertEqual(second_page.context['page_obj'].number, 2)
        self.assertEqual(len(second_page.context['review_cards']), 1)

        company_user = self.companies[0].user
        self.client.force_login(company_user)
        denied_response = self.client.get(reverse('admin_farmer_reviews'))
        self.assertEqual(denied_response.status_code, 302)
        self.assertIn(reverse('login'), denied_response.url)

        farmer_user = User.objects.create_user(username='admin-reviews-nonadmin-farmer', password='securepass123')
        Farmer.objects.create(
            user=farmer_user,
            full_name='Admin Reviews Farmer',
            phone='9765400999',
            aadhaar='2345600999',
            address='Private Farmer Address',
            income=25000,
            land_size=2,
        )
        self.client.force_login(farmer_user)
        denied_response = self.client.get(reverse('admin_farmer_reviews'))
        self.assertEqual(denied_response.status_code, 302)
        self.assertIn(reverse('login'), denied_response.url)


class PasswordChangeTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='password-admin',
            email='password-admin@example.com',
            password='OldSecurePass!2026',
        )

    def test_admin_dashboard_shows_change_password_link(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertContains(response, reverse('password_change'))
        self.assertContains(response, 'Change Password')

    def test_admin_can_change_password_and_remains_logged_in(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse('password_change'),
            {
                'old_password': 'OldSecurePass!2026',
                'new_password1': 'NewSecurePass!2026',
                'new_password2': 'NewSecurePass!2026',
            },
        )

        self.assertRedirects(response, reverse('admin_dashboard'))
        self.admin_user.refresh_from_db()
        self.assertTrue(self.admin_user.check_password('NewSecurePass!2026'))
        self.assertTrue(self.client.session.get('_auth_user_id'))


class AdminSchemeManagementTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='scheme-admin',
            email='scheme-admin@example.com',
            password='securepass123',
        )
        self.company_user = User.objects.create_user(
            username='scheme-company',
            email='scheme-company@example.com',
            password='securepass123',
        )
        self.company = Company.objects.create(
            user=self.company_user,
            company_name='Scheme Review Company',
            gst_number='987654321012345',
            is_approved=True,
        )
        self.scheme = Scheme.objects.create(
            company=self.company,
            name='Review Scheme',
            description='Scheme used to test feedback review.',
            subsidy_amount=1000,
        )

    def test_total_applications_links_to_company_scheme_feedback_list(self):
        farmer_user = User.objects.create_user(username='scheme-review-farmer', password='securepass123')
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name='Scheme Review Farmer',
            phone='9876543210',
            aadhaar='123456789012',
            address='Farm address',
            income=20000,
            land_size=2,
        )
        application = Application.objects.create(farmer=farmer, scheme=self.scheme)
        Feedback.objects.create(
            farmer=farmer,
            company=self.company,
            application=application,
            rating=2,
            comment='Poor service',
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertContains(response, 'href="#scheme-review"')
        self.assertContains(response, 'Scheme Feedback Review')
        self.assertContains(response, self.company.company_name)
        self.assertContains(response, self.scheme.name)
        self.assertContains(response, '2.0 / 5')
        self.assertContains(response, 'Block scheme')
        scheme_row = response.context['all_schemes'].get(pk=self.scheme.pk)
        self.assertEqual(scheme_row.feedback_count, 1)
        self.assertEqual(scheme_row.low_feedback_count, 1)

    def test_admin_can_block_low_rated_scheme_with_reason(self):
        farmer_user = User.objects.create_user(username='scheme-block-farmer', password='securepass123')
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name='Scheme Block Farmer',
            phone='9876543211',
            aadhaar='123456789013',
            address='Farm address',
            income=20000,
            land_size=2,
        )
        application = Application.objects.create(farmer=farmer, scheme=self.scheme)
        Feedback.objects.create(
            farmer=farmer,
            company=self.company,
            application=application,
            rating=1,
            comment='Unsafe product',
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse('block_scheme', args=[self.scheme.id]),
            {'reason': 'Repeated low ratings and safety complaints.'},
        )

        self.assertRedirects(response, reverse('admin_dashboard'))
        self.scheme.refresh_from_db()
        self.assertFalse(self.scheme.is_active)
        self.assertTrue(
            self.company_user.notifications.filter(
                message__contains='Repeated low ratings and safety complaints.'
            ).exists()
        )

    def test_scheme_block_rejects_missing_reason_or_no_low_ratings(self):
        farmer_user = User.objects.create_user(username='scheme-reject-farmer', password='securepass123')
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name='Scheme Reject Farmer',
            phone='9876543212',
            aadhaar='123456789014',
            address='Farm address',
            income=20000,
            land_size=2,
        )
        application = Application.objects.create(farmer=farmer, scheme=self.scheme)
        Feedback.objects.create(
            farmer=farmer,
            company=self.company,
            application=application,
            rating=5,
            comment='Good product',
        )
        self.client.force_login(self.admin_user)

        missing_reason = self.client.post(reverse('block_scheme', args=[self.scheme.id]), {'reason': ' '})
        no_low_rating = self.client.post(
            reverse('block_scheme', args=[self.scheme.id]),
            {'reason': 'Reviewing the scheme.'},
        )

        self.assertRedirects(missing_reason, reverse('admin_dashboard'))
        self.assertRedirects(no_low_rating, reverse('admin_dashboard'))
        self.scheme.refresh_from_db()
        self.assertTrue(self.scheme.is_active)

        dashboard = self.client.get(reverse('admin_dashboard'))
        self.assertContains(dashboard, 'Requires at least one low rating (1 or 2 stars).')
        self.assertContains(dashboard, 'Requires a 1- or 2-star review')


class AdminFarmerManagementTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='farmer-admin',
            email='farmer-admin@example.com',
            password='securepass123',
        )
        self.farmer_user = User.objects.create_user(
            username='reviewed-farmer',
            email='farmer@example.com',
            password='securepass123',
        )
        self.farmer = Farmer.objects.create(
            user=self.farmer_user,
            full_name='Reviewed Farmer',
            phone='9876543210',
            aadhaar='123456789012',
            address='Farm address',
            income=20000,
            land_size=2,
            redflag_count=3,
        )

    def test_total_users_link_lists_farmer_and_highlight_three_flags(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertContains(response, 'href="#farmer-list"')
        self.assertContains(response, self.farmer.full_name)
        self.assertContains(response, '3 - Review required')
        self.assertContains(response, 'name="reason"')

    def test_admin_can_block_farmer_with_reason(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse('block_farmer', args=[self.farmer.id]),
            {'reason': 'Three company fraud flags after application reviews.'},
        )

        self.assertRedirects(response, reverse('admin_dashboard'))
        self.farmer_user.refresh_from_db()
        self.assertFalse(self.farmer_user.is_active)
        self.assertTrue(
            self.farmer_user.notifications.filter(
                message__contains='Three company fraud flags after application reviews.'
            ).exists()
        )

    def test_farmer_block_requires_reason_and_post(self):
        self.client.force_login(self.admin_user)

        get_response = self.client.get(reverse('block_farmer', args=[self.farmer.id]))
        post_response = self.client.post(reverse('block_farmer', args=[self.farmer.id]), {'reason': ' '})

        self.assertEqual(get_response.status_code, 405)
        self.farmer_user.refresh_from_db()
        self.assertTrue(self.farmer_user.is_active)
        self.assertFalse(self.farmer_user.notifications.exists())


class AdminCategoryManagementTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='category-admin',
            email='category-admin@example.com',
            password='securepass123',
        )
        company_user = User.objects.create_user(username='category-company', password='securepass123')
        self.company = Company.objects.create(
            user=company_user,
            company_name='Category Company',
            gst_number='723456789012345',
            is_approved=True,
        )

    def test_admin_can_add_edit_and_delete_shared_categories(self):
        self.client.force_login(self.admin_user)

        add_response = self.client.post(
            reverse('manage_category'),
            {'action': 'add', 'name': 'Medicinal Plants'},
        )
        self.assertRedirects(add_response, f"{reverse('admin_dashboard')}#category-management")
        category = Category.objects.get(name='Medicinal Plants')
        admin_response = self.client.get(reverse('admin_dashboard'))
        self.assertContains(admin_response, 'Medicinal Plants')

        self.client.force_login(self.company.user)
        company_response = self.client.get(reverse('create_scheme'))
        self.assertContains(company_response, 'Medicinal Plants')
        farmer_response = self.client.get(reverse('farmer_register'))
        self.assertContains(farmer_response, 'Medicinal Plants')
        self.client.force_login(self.admin_user)

        farmer_user = User.objects.create_user(username='category-farmer', password='securepass123')
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name='Category Farmer',
            phone='9876543291',
            aadhaar='123456789091',
            address='Category farm address',
            income=20000,
            land_size=2,
        )
        scheme = Scheme.objects.create(
            company=self.company,
            name='Category Scheme',
            description='Category management test',
            subsidy_amount=1000,
        )
        farmer.categories.add(category)
        scheme.eligible_categories.add(category)

        edit_response = self.client.post(
            reverse('manage_category'),
            {'action': 'edit', 'category_id': category.id, 'name': 'Medicinal Crops'},
        )
        self.assertRedirects(edit_response, f"{reverse('admin_dashboard')}#category-management")
        category.refresh_from_db()
        self.assertEqual(category.name, 'Medicinal Crops')
        self.assertTrue(farmer.categories.filter(pk=category.pk).exists())
        self.assertTrue(scheme.eligible_categories.filter(pk=category.pk).exists())

        delete_response = self.client.post(
            reverse('manage_category'),
            {'action': 'delete', 'category_id': category.id},
        )
        self.assertRedirects(delete_response, f"{reverse('admin_dashboard')}#category-management")
        self.assertFalse(Category.objects.filter(pk=category.pk).exists())
        self.assertFalse(farmer.categories.filter(pk=category.pk).exists())
        self.assertFalse(scheme.eligible_categories.filter(pk=category.pk).exists())

    def test_category_management_rejects_duplicates_and_non_admins(self):
        Category.objects.create(name='Unique Category')
        self.client.force_login(self.admin_user)

        duplicate_response = self.client.post(
            reverse('manage_category'),
            {'action': 'add', 'name': 'unique category'},
        )
        self.assertRedirects(duplicate_response, f"{reverse('admin_dashboard')}#category-management")
        self.assertEqual(Category.objects.filter(name__iexact='unique category').count(), 1)
        self.assertEqual(self.client.get(reverse('manage_category')).status_code, 405)

        company_user = self.company.user
        self.client.force_login(company_user)
        denied_response = self.client.post(
            reverse('manage_category'),
            {'action': 'add', 'name': 'Unauthorized Category'},
        )
        self.assertEqual(denied_response.status_code, 302)
        self.assertFalse(Category.objects.filter(name='Unauthorized Category').exists())


class FarmerRegistrationValidationTests(TestCase):
    def _registration_data(self):
        return {
            'full_name': 'New Farmer Applicant',
            'phone': '9876543201',
            'email': 'new-farmer@example.com',
            'password': 'SecurePass!2026',
            'confirm_password': 'SecurePass!2026',
            'aadhaar': '123456789021',
            'country': 'India',
            'state': 'Kerala',
            'district': 'Ernakulam',
            'house_address': 'House 22',
            'pincode': '682002',
            'income': '25000',
            'land_area_value': '2',
            'land_area_unit': 'acre',
            'categories': ['Crop Farming'],
        }

    def test_successful_registration_redirects_to_login_without_authenticating(self):
        response = self.client.post(reverse('farmer_register'), self._registration_data())

        self.assertRedirects(response, reverse('login'))
        self.assertTrue(User.objects.filter(username='new-farmer@example.com').exists())
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_blocked_farmer_identity_cannot_be_reused(self):
        admin_user = User.objects.create_superuser(
            username='blocking-admin',
            email='blocking-admin@example.com',
            password='securepass123',
        )
        blocked_user = User.objects.create_user(
            username='blocked-farmer@example.com',
            email='blocked-farmer@example.com',
            password='securepass123',
        )
        blocked_farmer = Farmer.objects.create(
            user=blocked_user,
            full_name='Blocked Farmer',
            phone='9876543200',
            aadhaar='123456789020',
            address='House 10, Ernakulam, Kerala, India - 682001',
            income=25000,
            land_size=2,
        )
        self.client.force_login(admin_user)
        self.client.post(
            reverse('block_farmer', args=[blocked_farmer.id]),
            {'reason': 'Fraudulent application documents.'},
        )
        blocked_farmer.refresh_from_db()
        blocked_user.refresh_from_db()
        self.assertFalse(blocked_user.is_active)

        self.client.logout()
        original_identity = {
            'phone': blocked_farmer.phone,
            'aadhaar': blocked_farmer.aadhaar,
            'email': blocked_user.email,
        }
        for identity_field, duplicate_value in original_identity.items():
            registration_data = self._registration_data()
            registration_data[identity_field] = duplicate_value

            with self.subTest(identity_field=identity_field):
                response = self.client.post(reverse('farmer_register'), registration_data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(Farmer.objects.count(), 1)
                self.assertFalse(User.objects.filter(username='new-farmer@example.com').exists())

    def test_registration_rejects_invalid_email_and_weak_password(self):
        registration_data = self._registration_data()
        registration_data['email'] = 'not-an-email'
        registration_data['password'] = '123'
        registration_data['confirm_password'] = '123'

        response = self.client.post(reverse('farmer_register'), registration_data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='not-an-email').exists())
        self.assertFalse(Farmer.objects.exists())

    def test_registration_rejects_case_insensitive_duplicate_email(self):
        User.objects.create_user(
            username='Existing-Farmer@Example.com',
            email='Existing-Farmer@Example.com',
            password='securepass123',
        )
        registration_data = self._registration_data()
        registration_data['email'] = 'existing-farmer@example.com'

        response = self.client.post(reverse('farmer_register'), registration_data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Farmer.objects.exists())
        self.assertFalse(User.objects.filter(username='existing-farmer@example.com').exists())


class LandAreaTests(TestCase):
    def test_farmer_registration_stores_standardized_area_and_receipt(self):
        response = self.client.post(
            reverse('farmer_register'),
            {
                'full_name': 'Registered Farmer',
                'phone': '9876543288',
                'email': 'registered@example.com',
                'password': 'securepass123',
                'confirm_password': 'securepass123',
                'aadhaar': '123456789088',
                'country': 'India',
                'state': 'Kerala',
                'district': 'Ernakulam',
                'house_address': 'House 12',
                'pincode': '682001',
                'income': '25000',
                'land_area_value': '50',
                'land_area_unit': 'cent',
                'categories': ['Crop Farming'],
                'land_tax_receipt': SimpleUploadedFile('receipt.pdf', b'land record'),
            },
        )

        self.assertEqual(response.status_code, 302)
        farmer = Farmer.objects.get(phone='9876543288')
        self.assertEqual(farmer.land_area_unit, 'cent')
        self.assertEqual(farmer.land_area_sqm, Decimal('2023.428211200'))
        self.assertEqual(farmer.state.name, 'Kerala')
        self.assertEqual(farmer.district.name, 'Ernakulam')
        self.assertTrue(farmer.land_tax_receipt.name.startswith('land_tax_receipts/'))

    def test_supported_units_convert_to_square_metres(self):
        self.assertEqual(land_area_to_sqm(50, 'cent'), Decimal('2023.428211200'))
        self.assertEqual(land_area_to_sqm(4, 'are') + 5, Decimal('405'))

    def test_scheme_criteria_are_standardized_using_selected_unit(self):
        company_user = User.objects.create_user(username='land-company', password='securepass123')
        company = Company.objects.create(
            user=company_user,
            company_name='Land Company',
            gst_number='223456789012345',
            is_approved=True,
        )
        scheme = Scheme.objects.create(
            company=company,
            name='Cent Scheme',
            description='Land-based scheme',
            subsidy_amount=1000,
            min_land=25,
            max_land=75,
            land_area_unit='cent',
        )
        self.assertEqual(scheme.min_land_sqm, Decimal('1011.714105600'))
        self.assertEqual(scheme.max_land_sqm, Decimal('3035.142316800'))

    def test_dashboard_uses_standardized_area_for_cross_unit_eligibility(self):
        farmer_user = User.objects.create_user(username='land-farmer', password='securepass123')
        farmer = Farmer.objects.create(
            user=farmer_user,
            full_name='Land Farmer',
            phone='9876543299',
            aadhaar='123456789099',
            address='Test address',
            income=20000,
            land_size=0.5,
            land_area_value=50,
            land_area_unit='cent',
            land_area_sqm=land_area_to_sqm(50, 'cent'),
        )
        category, _ = Category.objects.get_or_create(name='Crop Farming')
        farmer.categories.add(category)
        company_user = User.objects.create_user(username='eligibility-company', password='securepass123')
        company = Company.objects.create(
            user=company_user,
            company_name='Eligibility Company',
            gst_number='323456789012345',
            is_approved=True,
        )
        scheme = Scheme.objects.create(
            company=company,
            name='Square Metre Scheme',
            description='Cross-unit scheme',
            subsidy_amount=1000,
            min_land=1000,
            max_land=3000,
            land_area_unit='sqm',
        )
        scheme.eligible_categories.add(category)
        newer_scheme = Scheme.objects.create(
            company=company,
            name='Newest Subsidy Scheme',
            description='Newest farmer subsidy',
            subsidy_amount=1500,
        )
        newer_scheme.eligible_categories.add(category)
        Scheme.objects.filter(id=scheme.id).update(created_at=timezone.now() - timedelta(days=1))
        Scheme.objects.filter(id=newer_scheme.id).update(created_at=timezone.now())

        self.client.force_login(farmer_user)
        response = self.client.get(reverse('farmer_dashboard'))

        self.assertContains(response, scheme.name)
        self.assertContains(response, newer_scheme.name)
        self.assertEqual(response.context['display_cards'][0]['title'], newer_scheme.name)
        self.assertContains(response, 'Latest Subsidy Notifications')
        self.assertContains(response, 'Actual Price')
        self.assertContains(response, 'Subsidy Price')
        self.assertContains(response, 'Valid Till:')
        self.assertContains(response, 'Apply Now')
        self.assertNotContains(response, 'Eligibility:')
        self.assertNotContains(response, 'Cross-unit scheme')


class LocationEligibilityTests(TestCase):
    def setUp(self):
        self.kerala = State.objects.get(name='Kerala')
        self.tamil_nadu = State.objects.get(name='Tamil Nadu')
        self.kottayam = District.objects.get(state=self.kerala, name='Kottayam')
        self.ernakulam = District.objects.get(state=self.kerala, name='Ernakulam')
        self.coimbatore = District.objects.get(state=self.tamil_nadu, name='Coimbatore')
        self.category = Category.objects.create(name='Equipment')

        company_user = User.objects.create_user(username='location-company', password='securepass123')
        self.company = Company.objects.create(
            user=company_user,
            company_name='Location Company',
            gst_number='423456789012345',
            is_approved=True,
        )
        self.scheme = Scheme.objects.create(
            company=self.company,
            name='Kerala Equipment Scheme',
            description='State-wide scheme',
            subsidy_amount=50000,
            geographical_scope='selected_states',
        )
        self.scheme.eligible_categories.add(self.category)
        SchemeLocation.objects.create(
            scheme=self.scheme,
            state=self.kerala,
            district=None,
        )

        self.farmer_user = User.objects.create_user(username='kottayam-farmer', password='securepass123')
        self.farmer = Farmer.objects.create(
            user=self.farmer_user,
            full_name='Kottayam Farmer',
            phone='9876543277',
            aadhaar='123456789077',
            address='Kottayam address',
            income=20000,
            land_size=4,
            state=self.kerala,
            district=self.kottayam,
        )
        self.farmer.categories.add(self.category)

        other_user = User.objects.create_user(username='ernakulam-farmer', password='securepass123')
        self.other_farmer = Farmer.objects.create(
            user=other_user,
            full_name='Ernakulam Farmer',
            phone='9876543276',
            aadhaar='123456789076',
            address='Ernakulam address',
            income=20000,
            land_size=4,
            state=self.kerala,
            district=self.ernakulam,
        )
        self.other_farmer.categories.add(self.category)

        ineligible_user = User.objects.create_user(username='tn-farmer', password='securepass123')
        self.ineligible_farmer = Farmer.objects.create(
            user=ineligible_user,
            full_name='Tamil Farmer',
            phone='9876543266',
            aadhaar='123456789066',
            address='Tamil Nadu address',
            income=20000,
            land_size=4,
            state=self.tamil_nadu,
            district=self.coimbatore,
        )
        self.ineligible_farmer.categories.add(self.category)

    def test_complete_location_catalog_is_seeded(self):
        self.assertEqual(State.objects.count(), len(LOCATION_DATA))
        self.assertEqual(
            District.objects.count(),
            sum(len(districts) for districts in LOCATION_DATA.values()),
        )

    def test_scheme_form_starts_with_all_states_and_kerala_districts(self):
        self.client.force_login(self.company.user)
        response = self.client.get(reverse('create_scheme'))
        self.assertContains(response, 'All States')
        self.assertContains(response, 'Kerala')
        self.assertContains(response, 'Kottayam')

    def test_farmer_profile_updates_state_and_matching_district(self):
        self.client.force_login(self.farmer_user)
        response = self.client.post(
            reverse('edit_farmer_profile'),
            {
                'full_name': self.farmer.full_name,
                'phone': self.farmer.phone,
                'address': 'Updated Coimbatore address',
                'state': str(self.tamil_nadu.id),
                'district': str(self.coimbatore.id),
                'income': '20000',
                'land_area_value': '4',
                'land_area_unit': 'acre',
                'categories': ['Equipment'],
            },
        )

        self.assertRedirects(response, reverse('farmer_dashboard'))
        self.farmer.refresh_from_db()
        self.assertEqual(self.farmer.state, self.tamil_nadu)
        self.assertEqual(self.farmer.district, self.coimbatore)

        dashboard_response = self.client.get(reverse('farmer_dashboard'))
        self.assertContains(dashboard_response, 'Tamil Nadu')
        self.assertContains(dashboard_response, 'Coimbatore')

    def test_farmer_profile_rejects_district_from_another_state(self):
        self.client.force_login(self.farmer_user)
        response = self.client.post(
            reverse('edit_farmer_profile'),
            {
                'full_name': self.farmer.full_name,
                'phone': self.farmer.phone,
                'address': 'Updated address',
                'state': str(self.kerala.id),
                'district': str(self.coimbatore.id),
                'income': '20000',
                'land_area_value': '4',
                'land_area_unit': 'acre',
                'categories': ['Equipment'],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.farmer.refresh_from_db()
        self.assertEqual(self.farmer.state, self.kerala)
        self.assertEqual(self.farmer.district, self.kottayam)

    def test_only_matching_state_sees_scheme_and_receives_notification(self):
        self.client.force_login(self.farmer_user)
        response = self.client.get(reverse('farmer_dashboard'))
        self.assertContains(response, self.scheme.name)

        notify_eligible_farmers(self.scheme)
        self.assertTrue(self.farmer_user.notifications.filter(message__contains=self.scheme.name).exists())
        self.assertTrue(self.other_farmer.user.notifications.filter(message__contains=self.scheme.name).exists())

    def test_ineligible_farmer_cannot_apply_by_direct_url(self):
        self.client.force_login(self.ineligible_farmer.user)
        response = self.client.get(reverse('apply_scheme', args=[self.scheme.id]))
        self.assertRedirects(response, reverse('farmer_dashboard'))
        self.assertFalse(Application.objects.filter(farmer=self.ineligible_farmer, scheme=self.scheme).exists())

    def test_company_scheme_creation_persists_state_location_and_notifies_match(self):
        self.client.force_login(self.company.user)
        response = self.client.post(
            reverse('create_scheme'),
            {
                'name': 'Kerala State Scheme',
                'description': 'State-wide equipment support',
                'subsidy_amount': '50000',
                'application_deadline': '2026-12-31',
                'eligible_categories': [str(self.category.id)],
                'geographical_scope': 'selected_states',
                'eligible_states': [str(self.kerala.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        scheme = Scheme.objects.get(name='Kerala State Scheme')
        self.assertEqual(scheme.geographical_scope, 'selected_states')
        self.assertEqual(str(scheme.application_deadline), '2026-12-31')
        self.assertTrue(SchemeLocation.objects.filter(scheme=scheme, state=self.kerala, district__isnull=True).exists())
        self.assertTrue(self.farmer_user.notifications.filter(message__contains=scheme.name).exists())
        self.assertTrue(self.other_farmer.user.notifications.filter(message__contains=scheme.name).exists())

    def test_selected_states_scheme_uses_state_level_eligibility_only(self):
        state_scheme = Scheme.objects.create(
            company=self.company,
            name='Kerala State Equipment Scheme',
            description='State-wide scheme',
            subsidy_amount=50000,
            geographical_scope='selected_states',
        )
        state_scheme.eligible_categories.add(self.category)
        SchemeLocation.objects.create(scheme=state_scheme, state=self.kerala, district=None)

        self.assertTrue(scheme_matches_farmer(state_scheme, self.farmer))
        self.assertTrue(scheme_matches_farmer(state_scheme, self.other_farmer))

        self.assertFalse(scheme_matches_farmer(state_scheme, self.ineligible_farmer))

    def test_all_states_scheme_uses_special_all_state_selection(self):
        self.client.force_login(self.company.user)
        response = self.client.post(
            reverse('create_scheme'),
            {
                'name': 'All India Equipment Scheme',
                'description': 'Country-wide scheme',
                'subsidy_amount': '50000',
                'application_deadline': '2026-12-31',
                'eligible_categories': [str(self.category.id)],
                'all_states': '1',
            },
        )

        self.assertEqual(response.status_code, 302)
        scheme = Scheme.objects.get(name='All India Equipment Scheme')
        self.assertEqual(scheme.geographical_scope, 'all_states')
        self.assertTrue(SchemeLocation.objects.filter(scheme=scheme, district__isnull=True).exists())
        self.assertTrue(scheme_matches_farmer(scheme, self.farmer))
        self.assertTrue(scheme_matches_farmer(scheme, self.ineligible_farmer))

    def test_farmer_can_view_only_eligible_subsidy_details(self):
        self.client.force_login(self.farmer_user)
        response = self.client.get(reverse('subsidy_details', args=[self.scheme.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.scheme.name)

        self.client.force_login(self.ineligible_farmer.user)
        response = self.client.get(reverse('subsidy_details', args=[self.scheme.id]))
        self.assertRedirects(response, reverse('farmer_dashboard'))

    def test_subsidy_details_shows_only_reviews_for_that_scheme(self):
        same_scheme_application = Application.objects.create(
            farmer=self.other_farmer,
            scheme=self.scheme,
            status='approved',
        )
        Feedback.objects.create(
            farmer=self.other_farmer,
            company=self.company,
            application=same_scheme_application,
            rating=4,
            comment='This scheme improved my harvest.',
        )
        other_scheme = Scheme.objects.create(
            company=self.company,
            name='Separate Scheme',
            description='A different subsidy scheme',
            subsidy_amount=1000,
        )
        other_application = Application.objects.create(
            farmer=self.ineligible_farmer,
            scheme=other_scheme,
            status='approved',
        )
        Feedback.objects.create(
            farmer=self.ineligible_farmer,
            company=self.company,
            application=other_application,
            rating=1,
            comment='This belongs to another scheme only.',
        )
        self.client.force_login(self.farmer_user)

        response = self.client.get(reverse('subsidy_details', args=[self.scheme.id]))

        self.assertContains(response, 'Farmer Reviews')
        self.assertContains(response, '4.0')
        self.assertContains(response, 'Based on 1 applicant review')
        self.assertContains(response, 'This scheme improved my harvest.')
        self.assertNotContains(response, 'This belongs to another scheme only.')
        self.assertNotContains(response, self.other_farmer.full_name)
        self.assertNotContains(response, self.other_farmer.phone)
        rendered_details = response.content.decode()
        reviews_shortcut_position = rendered_details.index('href="#scheme-reviews-title"')
        self.assertLess(rendered_details.index('Product Description'), reviews_shortcut_position)
        self.assertLess(reviews_shortcut_position, rendered_details.index('Actual Price'))

    def test_subsidy_details_shows_empty_review_state(self):
        self.client.force_login(self.farmer_user)

        response = self.client.get(reverse('subsidy_details', args=[self.scheme.id]))

        self.assertContains(response, 'Farmer Reviews')
        self.assertContains(response, 'No reviews yet.')
        self.assertIsNone(response.context['feedback_average'])
        self.assertEqual(response.context['feedback_count'], 0)

    def test_subsidy_details_carousel_includes_all_scheme_reviews(self):
        for index, rating in enumerate((5, 4, 3, 2), start=1):
            application = Application.objects.create(
                farmer=self.other_farmer,
                scheme=self.scheme,
                status='approved',
            )
            Feedback.objects.create(
                farmer=self.other_farmer,
                company=self.company,
                application=application,
                rating=rating,
                comment=f'Carousel review {index}.',
            )
        self.client.force_login(self.farmer_user)

        response = self.client.get(reverse('subsidy_details', args=[self.scheme.id]))

        self.assertEqual(response.context['feedback_count'], 4)
        self.assertEqual(response.context['feedback_average'], 3.5)
        self.assertEqual(len(response.context['scheme_review_cards']), 4)
        self.assertContains(response, 'data-scheme-reviews-carousel')
        self.assertContains(response, 'data-review-previous')
        self.assertContains(response, 'data-review-next')
        for index in range(1, 5):
            self.assertContains(response, f'Carousel review {index}.')

    def test_subsidy_details_show_district_and_state_for_selected_districts(self):
        district_scheme = Scheme.objects.create(
            company=self.company,
            name='Kerala District Equipment Scheme',
            description='District-specific scheme',
            subsidy_amount=50000,
            geographical_scope='selected_states',
        )
        district_scheme.eligible_categories.add(self.category)
        SchemeLocation.objects.create(scheme=district_scheme, state=self.kerala, district=self.kottayam)
        SchemeLocation.objects.create(scheme=district_scheme, state=self.kerala, district=self.ernakulam)

        self.client.force_login(self.farmer_user)
        response = self.client.get(reverse('subsidy_details', args=[district_scheme.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Kottayam, Kerala')
        self.assertContains(response, 'Ernakulam, Kerala')
        self.assertNotContains(response, '<li>Kerala</li>')
        self.assertNotContains(response, 'All districts within the selected states are eligible.')

    def test_standalone_farmer_feedback_page_is_removed(self):
        self.client.force_login(self.farmer_user)
        response = self.client.get('/farmer/feedback/')

        self.assertEqual(response.status_code, 404)

    def test_company_can_store_scheme_image_and_actual_price(self):
        self.client.force_login(self.company.user)
        response = self.client.post(
            reverse('create_scheme'),
            {
                'name': 'Uploaded Product Scheme',
                'description': 'Product with uploaded image',
                'subsidy_amount': '50000',
                'actual_price': '75000',
                'application_deadline': '2026-12-31',
                'eligible_categories': [str(self.category.id)],
                'eligible_states': [str(self.kerala.id)],
                'scheme_image': SimpleUploadedFile('product.jpg', b'image data', content_type='image/jpeg'),
            },
        )

        self.assertEqual(response.status_code, 302)
        scheme = Scheme.objects.get(name='Uploaded Product Scheme')
        self.assertEqual(scheme.actual_price, Decimal('75000.00'))
        self.assertTrue(scheme.scheme_image.name.startswith('scheme_images/'))

    def test_invalid_scheme_numbers_return_form_error(self):
        self.client.force_login(self.company.user)
        response = self.client.post(
            reverse('create_scheme'),
            {
                'name': 'Invalid Scheme',
                'description': 'Invalid numeric input',
                'subsidy_amount': 'not-a-number',
                'eligible_categories': [str(self.category.id)],
                'all_states': '1',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Subsidy amount must be a valid number.')
        self.assertFalse(Scheme.objects.filter(name='Invalid Scheme').exists())

    def test_scheme_creation_requires_a_valid_future_application_deadline(self):
        self.client.force_login(self.company.user)
        scheme_data = {
            'name': 'Deadline Validation Scheme',
            'description': 'A scheme with a required deadline',
            'subsidy_amount': '50000',
            'eligible_categories': [str(self.category.id)],
            'all_states': '1',
        }

        missing_deadline_response = self.client.post(reverse('create_scheme'), scheme_data)
        self.assertEqual(missing_deadline_response.status_code, 200)
        self.assertContains(missing_deadline_response, 'Last date for applications is required.')

        scheme_data['application_deadline'] = 'invalid-date'
        invalid_deadline_response = self.client.post(reverse('create_scheme'), scheme_data)
        self.assertEqual(invalid_deadline_response.status_code, 200)
        self.assertContains(invalid_deadline_response, 'Enter a valid application deadline.')

        scheme_data['application_deadline'] = '2020-01-01'
        expired_deadline_response = self.client.post(reverse('create_scheme'), scheme_data)
        self.assertEqual(expired_deadline_response.status_code, 200)
        self.assertContains(expired_deadline_response, 'Application deadline cannot be in the past.')
        self.assertFalse(Scheme.objects.filter(name='Deadline Validation Scheme').exists())

    def test_company_can_extend_application_deadline_when_editing_scheme(self):
        self.scheme.application_deadline = timezone.localdate() + timedelta(days=3)
        self.scheme.save(update_fields=['application_deadline'])
        self.client.force_login(self.company.user)

        response = self.client.post(
            reverse('edit_scheme', args=[self.scheme.id]),
            {
                'name': self.scheme.name,
                'description': self.scheme.description,
                'subsidy_amount': str(self.scheme.subsidy_amount),
                'application_deadline': '2026-12-31',
                'eligible_categories': [str(self.category.id)],
                'eligible_states': [str(self.kerala.id)],
            },
        )

        self.assertRedirects(response, reverse('scheme_details', args=[self.scheme.id]))
        self.scheme.refresh_from_db()
        self.assertEqual(str(self.scheme.application_deadline), '2026-12-31')

    def test_expired_scheme_cannot_be_applied_by_direct_url(self):
        self.scheme.application_deadline = timezone.localdate() - timedelta(days=1)
        self.scheme.save(update_fields=['application_deadline'])
        self.client.force_login(self.farmer_user)

        response = self.client.get(reverse('apply_scheme', args=[self.scheme.id]))

        self.assertRedirects(response, reverse('farmer_dashboard'))
        self.assertFalse(Application.objects.filter(farmer=self.farmer, scheme=self.scheme).exists())
        dashboard_response = self.client.get(reverse('farmer_dashboard'))
        self.assertContains(dashboard_response, 'Applications Closed')

    def test_company_can_view_edit_and_delete_its_scheme(self):
        self.client.force_login(self.company.user)

        detail_response = self.client.get(reverse('scheme_details', args=[self.scheme.id]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, self.scheme.name)

        edit_response = self.client.post(
            reverse('edit_scheme', args=[self.scheme.id]),
            {
                'name': 'Updated Equipment Scheme',
                'description': 'Updated description',
                'subsidy_amount': '60000',
                'application_deadline': '2026-12-31',
                'eligible_categories': [str(self.category.id)],
                'eligible_states': [str(self.kerala.id)],
            },
        )
        self.assertRedirects(edit_response, reverse('scheme_details', args=[self.scheme.id]))
        self.scheme.refresh_from_db()
        self.assertEqual(self.scheme.name, 'Updated Equipment Scheme')
        self.assertTrue(self.scheme.locations.filter(state=self.kerala, district__isnull=True).exists())

        delete_response = self.client.post(reverse('delete_scheme', args=[self.scheme.id]))
        self.assertRedirects(delete_response, reverse('company_dashboard'))
        self.assertFalse(Scheme.objects.filter(id=self.scheme.id).exists())

    def test_company_scheme_management_is_owner_restricted_and_delete_is_post_only(self):
        other_user = User.objects.create_user(username='other-company', password='securepass123')
        other_company = Company.objects.create(
            user=other_user,
            company_name='Other Company',
            gst_number='523456789012345',
            is_approved=True,
        )
        self.client.force_login(other_user)

        self.assertEqual(self.client.get(reverse('scheme_details', args=[self.scheme.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse('edit_scheme', args=[self.scheme.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse('delete_scheme', args=[self.scheme.id])).status_code, 404)
        self.assertTrue(Scheme.objects.filter(id=self.scheme.id, company=self.company).exists())
        self.assertFalse(Scheme.objects.filter(company=other_company).exists())


class ApplySchemeValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='farmer1', password='securepass123')
        self.farmer = Farmer.objects.create(
            user=self.user,
            full_name='Test Farmer',
            phone='9876543210',
            aadhaar='123456789012',
            address='Test address',
            income=20000,
            land_size=4,
        )

        self.company_user = User.objects.create_user(username='company1', password='securepass123')
        self.company = Company.objects.create(
            user=self.company_user,
            company_name='Test Company',
            gst_number='123456789012345',
            is_approved=True,
        )
        self.scheme = Scheme.objects.create(
            company=self.company,
            name='Test Scheme',
            description='A sample scheme',
            subsidy_amount=50000,
        )

    def _application_submission_data(self):
        return {
            'purpose': 'Need subsidy support',
            'aadhaar_card': SimpleUploadedFile('aadhaar.pdf', b'aadhaar card'),
            'income_certificate': SimpleUploadedFile('income.pdf', b'income certificate'),
            'land_tax_document': SimpleUploadedFile('land.pdf', b'land tax document'),
        }

    def test_dashboard_displays_average_rating_from_scheme_feedback(self):
        category, _ = Category.objects.get_or_create(name='Crop Farming')
        self.farmer.categories.add(category)
        self.scheme.eligible_categories.add(category)

        for username, phone, aadhaar, rating in [
            ('rating-farmer-one', '9876543211', '123456789013', 4),
            ('rating-farmer-two', '9876543212', '123456789014', 5),
        ]:
            feedback_user = User.objects.create_user(username=username, password='securepass123')
            feedback_farmer = Farmer.objects.create(
                user=feedback_user,
                full_name=username,
                phone=phone,
                aadhaar=aadhaar,
                address='Feedback farmer address',
                income=20000,
                land_size=4,
            )
            application = Application.objects.create(
                farmer=feedback_farmer,
                scheme=self.scheme,
                status='approved',
            )
            Feedback.objects.create(
                farmer=feedback_farmer,
                company=self.company,
                application=application,
                rating=rating,
                comment='Verified scheme feedback',
            )

        self.client.force_login(self.user)
        response = self.client.get(reverse('farmer_dashboard'))

        self.assertContains(response, '4.5')
        self.assertContains(response, '(2 reviews)')

    def test_dashboard_shows_no_ratings_yet_when_scheme_has_no_feedback(self):
        category, _ = Category.objects.get_or_create(name='Crop Farming')
        self.farmer.categories.add(category)
        self.scheme.eligible_categories.add(category)
        self.client.force_login(self.user)

        response = self.client.get(reverse('farmer_dashboard'))

        self.assertContains(response, 'No ratings yet')
        card = response.context['display_cards'][0]
        self.assertIsNone(card['feedback_average'])
        self.assertEqual(card['feedback_count'], 0)

    def test_successful_submission_generates_unique_six_digit_application_number(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('apply_scheme', args=[self.scheme.id]),
            self._application_submission_data(),
            follow=True,
        )

        application = Application.objects.get()
        self.assertEqual(response.status_code, 200)
        self.assertRegex(application.application_number, r'^\d{6}$')
        self.assertContains(response, application.application_number)
        self.assertContains(response, 'Pending Verification')

    def test_company_can_view_uploaded_application_documents(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse('apply_scheme', args=[self.scheme.id]),
            self._application_submission_data(),
        )

        self.client.force_login(self.company_user)
        response = self.client.get(reverse('view_applications_scheme', args=[self.scheme.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Uploaded Documents:')
        self.assertContains(response, '/media/application_docs/')
        self.assertContains(response, 'Aadhaar Card')
        self.assertContains(response, 'Income Certificate')
        self.assertContains(response, 'Land Tax Document')
        self.assertContains(response, self.farmer.aadhaar)
        self.assertContains(response, self.farmer.address)

    def test_company_can_approve_application_from_filtered_scheme_page(self):
        application = Application.objects.create(
            farmer=self.farmer,
            scheme=self.scheme,
            status='pending',
        )
        self.client.force_login(self.company_user)

        response = self.client.post(
            reverse('view_applications_scheme', args=[self.scheme.id]),
            {'application_id': application.id, 'action': 'approve'},
        )

        self.assertRedirects(response, reverse('view_applications_scheme', args=[self.scheme.id]))
        application.refresh_from_db()
        self.assertEqual(application.status, 'approved')

    def test_repeated_company_redflag_does_not_increment_farmer_twice(self):
        application = Application.objects.create(
            farmer=self.farmer,
            scheme=self.scheme,
            status='pending',
        )
        self.client.force_login(self.company_user)
        review_url = reverse('view_applications_scheme', args=[self.scheme.id])
        review_data = {
            'application_id': application.id,
            'action': 'redflag',
            'reason': 'Fraudulent application details.',
        }

        self.client.post(review_url, review_data)
        self.client.post(review_url, review_data)

        self.farmer.refresh_from_db()
        self.assertEqual(self.farmer.redflag_count, 1)

    def test_each_successful_submission_gets_a_different_number(self):
        self.client.force_login(self.user)

        self.client.post(reverse('apply_scheme', args=[self.scheme.id]), self._application_submission_data())
        second_scheme = Scheme.objects.create(
            company=self.company,
            name='Second Scheme',
            description='Another sample scheme',
            subsidy_amount=25000,
        )
        self.client.post(reverse('apply_scheme', args=[second_scheme.id]), self._application_submission_data())

        numbers = list(Application.objects.values_list('application_number', flat=True))
        self.assertEqual(len(numbers), 2)
        self.assertEqual(len(set(numbers)), 2)

    def test_fraud_termination_keeps_application_number(self):
        self.farmer.land_area_sqm = Decimal('300000')
        self.farmer.save(update_fields=['land_area_sqm'])
        self.client.force_login(self.user)

        self.client.post(reverse('apply_scheme', args=[self.scheme.id]), self._application_submission_data())

        application = Application.objects.get()
        self.assertEqual(application.status, 'fraud')
        self.assertRegex(application.application_number, r'^\d{6}$')

    def test_application_requires_aadhaar_income_and_land_tax_documents(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('apply_scheme', args=[self.scheme.id]),
            {'purpose': 'Need subsidy support'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Aadhaar card, income certificate, and land tax document are mandatory.')
        self.assertFalse(Application.objects.exists())

    def test_farmer_dashboard_excludes_other_feedback_for_scheme_and_company(self):
        other_user = User.objects.create_user(username='farmer2', password='securepass123')
        other_farmer = Farmer.objects.create(
            user=other_user,
            full_name='Other Farmer',
            phone='9876543211',
            aadhaar='123456789013',
            address='Other address',
            income=30000,
            land_size=5,
        )
        other_application = Application.objects.create(
            farmer=other_farmer,
            scheme=self.scheme,
            status='approved',
        )
        Feedback.objects.create(
            farmer=other_farmer,
            company=self.company,
            application=other_application,
            rating=5,
            comment='Very helpful company and scheme.',
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse('farmer_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Previous Feedback from Other Farmers')
        self.assertNotContains(response, 'Very helpful company and scheme.')
        self.assertNotContains(response, 'Other Farmer')
        self.assertNotContains(response, '5/5')

    def test_farmer_dashboard_shows_self_feedback(self):
        application = Application.objects.create(
            farmer=self.farmer,
            scheme=self.scheme,
            status='approved',
        )
        Feedback.objects.create(
            farmer=self.farmer,
            company=self.company,
            application=application,
            rating=4,
            comment='The process was smooth and transparent.',
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse('farmer_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'My Feedback')
        self.assertContains(response, 'The process was smooth and transparent.')
        self.assertContains(response, '4/5')

    def test_farmer_dashboard_hides_other_feedback_and_feedback_form_accepts_images(self):
        other_user = User.objects.create_user(username='farmer2', password='securepass123')
        other_farmer = Farmer.objects.create(
            user=other_user,
            full_name='Other Farmer',
            phone='9876543211',
            aadhaar='123456789013',
            address='Other address',
            income=30000,
            land_size=5,
        )
        other_application = Application.objects.create(
            farmer=other_farmer,
            scheme=self.scheme,
            status='approved',
        )
        Feedback.objects.create(
            farmer=other_farmer,
            company=self.company,
            application=other_application,
            rating=5,
            comment='Very helpful company and scheme.',
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse('farmer_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Previous Feedback from Other Farmers')
        self.assertNotContains(response, 'Very helpful company and scheme.')

        app = Application.objects.create(
            farmer=self.farmer,
            scheme=self.scheme,
            status='approved',
        )
        feedback_response = self.client.get(reverse('give_feedback', args=[app.id]))
        self.assertContains(feedback_response, 'name="photos"')
        self.assertContains(feedback_response, 'multiple')
