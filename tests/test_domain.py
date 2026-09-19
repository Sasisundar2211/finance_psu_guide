from datetime import timedelta
from io import StringIO

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, models as dj_models, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import (
    Attempt,
    AttemptAnswer,
    Book,
    Chapter,
    Course,
    CourseMockTest,
    Enrollment,
    MockTest,
    MockTestEnrollment,
    MockTestPricingPlan,
    OptionChoice,
    Order,
    OrderStatus,
    PricingPlan,
    Question,
    UserChapterProgress,
    UserProfile,
)

PROJECT_MODEL_NAMES = {
    "UserProfile",
    "Course",
    "PricingPlan",
    "Book",
    "Chapter",
    "UserChapterProgress",
    "MockTest",
    "MockTestPricingPlan",
    "CourseMockTest",
    "Question",
    "Attempt",
    "AttemptAnswer",
    "Order",
    "Enrollment",
    "MockTestEnrollment",
    "BlogPost",
}


def make_user(username="student1", **kwargs):
    return User.objects.create_user(username=username, password="testpass123", **kwargs)


def make_course(**kwargs):
    defaults = dict(
        title="Course",
        short_description="Short",
        full_description="Full",
        is_published=True,
    )
    defaults.update(kwargs)
    return Course.objects.create(**defaults)


def make_pricing_plan(course=None, **kwargs):
    course = course or make_course()
    defaults = dict(course=course, duration_months=3, price_paise=100000)
    defaults.update(kwargs)
    return PricingPlan.objects.create(**defaults)


def make_book(course=None, **kwargs):
    course = course or make_course()
    defaults = dict(course=course, title="Book 1", order=1)
    defaults.update(kwargs)
    return Book.objects.create(**defaults)


def make_chapter(book=None, **kwargs):
    book = book or make_book()
    defaults = dict(
        book=book,
        title="Chapter 1",
        order=1,
        pdf_object_key="courses/1/books/1/chapters/1.pdf",
    )
    defaults.update(kwargs)
    return Chapter.objects.create(**defaults)


def make_mock_test(**kwargs):
    defaults = dict(title="Mock Test", duration_minutes=60, is_published=True)
    defaults.update(kwargs)
    return MockTest.objects.create(**defaults)


def make_mock_test_pricing_plan(mock_test=None, **kwargs):
    mock_test = mock_test or make_mock_test()
    defaults = dict(mock_test=mock_test, price_paise=50000)
    defaults.update(kwargs)
    return MockTestPricingPlan.objects.create(**defaults)


def question_kwargs(**overrides):
    kwargs = dict(
        text="What is 2+2?",
        option_a="3",
        option_b="4",
        option_c="5",
        option_d="6",
        correct_option=OptionChoice.B,
    )
    kwargs.update(overrides)
    return kwargs


class ModelInventoryTests(TestCase):
    def test_all_16_project_models_exist(self):
        core_model_names = {m.__name__ for m in apps.get_app_config("core").get_models()}
        self.assertEqual(core_model_names, PROJECT_MODEL_NAMES)
        self.assertEqual(len(PROJECT_MODEL_NAMES), 16)

    def test_no_topic_model(self):
        core_model_names = {m.__name__ for m in apps.get_app_config("core").get_models()}
        self.assertNotIn("Topic", core_model_names)

    def test_get_user_model_is_stock_user(self):
        self.assertIs(get_user_model(), User)

    def test_no_auth_user_model_override(self):
        self.assertEqual(settings.AUTH_USER_MODEL, "auth.User")


class UserProfileTests(TestCase):
    def test_one_profile_maximum_per_user(self):
        user = make_user()
        # Phase 3's post_save hook already created this user's profile.
        self.assertTrue(UserProfile.objects.filter(user=user).exists())
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserProfile.objects.create(user=user)

    def test_phone_number_max_length_is_15(self):
        field = UserProfile._meta.get_field("phone_number")
        self.assertEqual(field.max_length, 15)


class UserChapterProgressTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.chapter = make_chapter()

    def test_default_is_completed_false(self):
        progress = UserChapterProgress.objects.create(user=self.user, chapter=self.chapter)
        self.assertFalse(progress.is_completed)

    def test_duplicate_user_chapter_fails_at_db_level(self):
        UserChapterProgress.objects.create(user=self.user, chapter=self.chapter)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserChapterProgress.objects.create(user=self.user, chapter=self.chapter)


class CourseMockTestTests(TestCase):
    def test_duplicate_course_mock_test_fails_at_db_level(self):
        course = make_course()
        mock_test = make_mock_test()
        CourseMockTest.objects.create(course=course, mock_test=mock_test, order=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CourseMockTest.objects.create(course=course, mock_test=mock_test, order=2)


class QuestionTargetConstraintTests(TestCase):
    def setUp(self):
        self.mock_test = make_mock_test()
        self.chapter = make_chapter()

    def test_mock_test_only_succeeds(self):
        q = Question.objects.create(mock_test=self.mock_test, **question_kwargs())
        self.assertIsNone(q.chapter)

    def test_chapter_only_succeeds(self):
        q = Question.objects.create(chapter=self.chapter, **question_kwargs())
        self.assertIsNone(q.mock_test)

    def test_both_populated_fails_with_integrity_error(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Question.objects.create(
                    mock_test=self.mock_test, chapter=self.chapter, **question_kwargs()
                )

    def test_neither_populated_fails_with_integrity_error(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Question.objects.create(**question_kwargs())


class AttemptAnswerTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.mock_test = make_mock_test()
        self.attempt = Attempt.objects.create(
            student=self.user, mock_test=self.mock_test, time_limit_minutes=60
        )
        self.question = Question.objects.create(
            mock_test=self.mock_test, **question_kwargs()
        )

    def test_duplicate_attempt_question_fails_at_db_level(self):
        AttemptAnswer.objects.create(
            attempt=self.attempt, question=self.question, selected_option=OptionChoice.A
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AttemptAnswer.objects.create(
                    attempt=self.attempt,
                    question=self.question,
                    selected_option=OptionChoice.B,
                )

    def test_selected_option_may_be_null(self):
        answer = AttemptAnswer.objects.create(
            attempt=self.attempt, question=self.question, selected_option=None
        )
        self.assertIsNone(answer.selected_option)


class OrderConstraintTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.course_plan = make_pricing_plan()
        self.mock_plan = make_mock_test_pricing_plan()

    def test_course_plan_only_succeeds(self):
        order = Order.objects.create(
            student=self.user,
            course_pricing_plan=self.course_plan,
            razorpay_order_id="order_course_only",
            amount_paise=self.course_plan.price_paise,
        )
        self.assertIsNone(order.mock_test_pricing_plan)

    def test_mock_test_plan_only_succeeds(self):
        order = Order.objects.create(
            student=self.user,
            mock_test_pricing_plan=self.mock_plan,
            razorpay_order_id="order_mocktest_only",
            amount_paise=self.mock_plan.price_paise,
        )
        self.assertIsNone(order.course_pricing_plan)

    def test_both_plans_set_fails_at_db_level(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(
                    student=self.user,
                    course_pricing_plan=self.course_plan,
                    mock_test_pricing_plan=self.mock_plan,
                    razorpay_order_id="order_both_plans",
                    amount_paise=100000,
                )

    def test_neither_plan_set_fails_at_db_level(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(
                    student=self.user,
                    razorpay_order_id="order_no_plan",
                    amount_paise=100000,
                )

    def test_duplicate_razorpay_order_id_fails(self):
        Order.objects.create(
            student=self.user,
            course_pricing_plan=self.course_plan,
            razorpay_order_id="order_dup",
            amount_paise=100000,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(
                    student=self.user,
                    course_pricing_plan=self.course_plan,
                    razorpay_order_id="order_dup",
                    amount_paise=100000,
                )

    def test_duplicate_nonnull_razorpay_payment_id_fails(self):
        Order.objects.create(
            student=self.user,
            course_pricing_plan=self.course_plan,
            razorpay_order_id="order_pay_1",
            razorpay_payment_id="pay_dup",
            amount_paise=100000,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(
                    student=self.user,
                    course_pricing_plan=self.course_plan,
                    razorpay_order_id="order_pay_2",
                    razorpay_payment_id="pay_dup",
                    amount_paise=100000,
                )

    def test_multiple_null_razorpay_payment_id_values_are_valid(self):
        first = Order.objects.create(
            student=self.user,
            course_pricing_plan=self.course_plan,
            razorpay_order_id="order_null_1",
            amount_paise=100000,
        )
        second = Order.objects.create(
            student=self.user,
            course_pricing_plan=self.course_plan,
            razorpay_order_id="order_null_2",
            amount_paise=100000,
        )
        self.assertIsNone(first.razorpay_payment_id)
        self.assertIsNone(second.razorpay_payment_id)

    def test_currency_defaults_to_inr(self):
        order = Order.objects.create(
            student=self.user,
            course_pricing_plan=self.course_plan,
            razorpay_order_id="order_currency",
            amount_paise=100000,
        )
        self.assertEqual(order.currency, "INR")

    def test_status_defaults_to_created(self):
        order = Order.objects.create(
            student=self.user,
            course_pricing_plan=self.course_plan,
            razorpay_order_id="order_status",
            amount_paise=100000,
        )
        self.assertEqual(order.status, OrderStatus.CREATED)


class EnrollmentUniqueOrderTests(TestCase):
    def test_second_enrollment_same_order_fails(self):
        user = make_user()
        course = make_course()
        plan = make_pricing_plan(course=course)
        order = Order.objects.create(
            student=user,
            course_pricing_plan=plan,
            razorpay_order_id="order_enroll_1",
            amount_paise=plan.price_paise,
            status=OrderStatus.VERIFIED,
        )
        Enrollment.objects.create(
            student=user,
            course=course,
            pricing_plan=plan,
            expires_at=timezone.now() + timedelta(days=90),
            order=order,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Enrollment.objects.create(
                    student=user,
                    course=course,
                    pricing_plan=plan,
                    expires_at=timezone.now() + timedelta(days=90),
                    order=order,
                )


class MockTestEnrollmentUniqueOrderTests(TestCase):
    def test_second_mock_test_enrollment_same_order_fails(self):
        user = make_user()
        mock_test = make_mock_test()
        plan = make_mock_test_pricing_plan(mock_test=mock_test)
        order = Order.objects.create(
            student=user,
            mock_test_pricing_plan=plan,
            razorpay_order_id="order_mte_1",
            amount_paise=plan.price_paise,
            status=OrderStatus.VERIFIED,
        )
        MockTestEnrollment.objects.create(student=user, mock_test=mock_test, order=order)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MockTestEnrollment.objects.create(
                    student=user, mock_test=mock_test, order=order
                )


class MoneyRepresentationTests(TestCase):
    def test_price_and_amount_fields_are_integer_not_decimal_or_float(self):
        integer_field_specs = [
            (PricingPlan, "price_paise"),
            (MockTestPricingPlan, "price_paise"),
            (Order, "amount_paise"),
        ]
        for model, field_name in integer_field_specs:
            field = model._meta.get_field(field_name)
            self.assertIsInstance(field, dj_models.PositiveIntegerField)
            self.assertNotIsInstance(field, dj_models.DecimalField)
            self.assertNotIsInstance(field, dj_models.FloatField)


class ChapterPdfPersistenceTests(TestCase):
    def test_pdf_object_key_is_plain_string_field_not_file_or_image(self):
        field = Chapter._meta.get_field("pdf_object_key")
        self.assertIsInstance(field, dj_models.CharField)
        self.assertNotIsInstance(field, dj_models.FileField)
        self.assertNotIsInstance(field, dj_models.ImageField)


class MigrationConsistencyTests(TestCase):
    def test_no_pending_model_changes(self):
        out = StringIO()
        try:
            call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
        except SystemExit:
            self.fail(f"Pending model changes detected:\n{out.getvalue()}")
