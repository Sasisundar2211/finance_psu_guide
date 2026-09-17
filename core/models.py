from django.conf import settings
from django.db import models
from django.db.models import Q


class OptionChoice(models.TextChoices):
    A = "A", "Option A"
    B = "B", "Option B"
    C = "C", "Option C"
    D = "D", "Option D"


class OrderStatus(models.TextChoices):
    CREATED = "created", "Created"
    VERIFIED = "verified", "Verified"
    FAILED = "failed", "Failed"


class DurationMonths(models.IntegerChoices):
    THREE = 3, "3 months"
    SIX = 6, "6 months"
    TWELVE = 12, "12 months"


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    active_session_key = models.CharField(max_length=40, null=True, blank=True)
    phone_number = models.CharField(max_length=15, null=True, blank=True)

    def __str__(self):
        return f"Profile: {self.user}"


class Course(models.Model):
    title = models.CharField(max_length=255)
    short_description = models.CharField(max_length=500)
    full_description = models.TextField()
    thumbnail = models.CharField(max_length=500, blank=True, default="")
    subjects_covered = models.JSONField(default=list, blank=True)
    is_published = models.BooleanField(default=False)

    def __str__(self):
        return self.title


class PricingPlan(models.Model):
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="pricing_plans"
    )
    duration_months = models.PositiveSmallIntegerField(choices=DurationMonths.choices)
    price_paise = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.course} — {self.duration_months} months"


class Book(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="books")
    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.course} — {self.title}"


class Chapter(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="chapters")
    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)
    pdf_object_key = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.book} — {self.title}"


class UserChapterProgress(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chapter_progress"
    )
    chapter = models.ForeignKey(
        Chapter, on_delete=models.CASCADE, related_name="progress_rows"
    )
    is_completed = models.BooleanField(default=False)
    last_accessed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "chapter"],
                name="userchapterprogress_unique_user_chapter",
            ),
        ]

    def __str__(self):
        return f"{self.user} — {self.chapter}"


class MockTest(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    duration_minutes = models.PositiveIntegerField()
    is_published = models.BooleanField(default=False)

    def __str__(self):
        return self.title


class MockTestPricingPlan(models.Model):
    mock_test = models.ForeignKey(
        MockTest, on_delete=models.CASCADE, related_name="pricing_plans"
    )
    price_paise = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.mock_test} pricing"


class CourseMockTest(models.Model):
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="course_mock_tests"
    )
    mock_test = models.ForeignKey(
        MockTest, on_delete=models.CASCADE, related_name="course_mock_tests"
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "mock_test"],
                name="coursemocktest_unique_course_mock_test",
            ),
        ]

    def __str__(self):
        return f"{self.course} ⇄ {self.mock_test}"


class Question(models.Model):
    mock_test = models.ForeignKey(
        MockTest,
        on_delete=models.CASCADE,
        related_name="questions",
        null=True,
        blank=True,
    )
    chapter = models.ForeignKey(
        Chapter,
        on_delete=models.CASCADE,
        related_name="questions",
        null=True,
        blank=True,
    )
    text = models.TextField()
    option_a = models.CharField(max_length=500)
    option_b = models.CharField(max_length=500)
    option_c = models.CharField(max_length=500)
    option_d = models.CharField(max_length=500)
    correct_option = models.CharField(max_length=1, choices=OptionChoice.choices)
    explanation = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(mock_test__isnull=False, chapter__isnull=True)
                    | Q(mock_test__isnull=True, chapter__isnull=False)
                ),
                name="question_exactly_one_target",
            ),
        ]

    def __str__(self):
        return self.text[:50]


class Attempt(models.Model):
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attempts"
    )
    mock_test = models.ForeignKey(
        MockTest, on_delete=models.PROTECT, related_name="attempts"
    )
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    time_limit_minutes = models.PositiveIntegerField()
    score = models.PositiveIntegerField(null=True, blank=True)
    total_questions = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.student} — {self.mock_test}"


class AttemptAnswer(models.Model):
    attempt = models.ForeignKey(
        Attempt, on_delete=models.CASCADE, related_name="answers"
    )
    question = models.ForeignKey(
        Question, on_delete=models.PROTECT, related_name="answers"
    )
    selected_option = models.CharField(
        max_length=1, choices=OptionChoice.choices, null=True, blank=True
    )
    saved_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["attempt", "question"],
                name="attemptanswer_unique_attempt_question",
            ),
        ]

    def __str__(self):
        return f"{self.attempt} — Q{self.question_id}"


class Order(models.Model):
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders"
    )
    course_pricing_plan = models.ForeignKey(
        PricingPlan,
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
    )
    mock_test_pricing_plan = models.ForeignKey(
        MockTestPricingPlan,
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
    )
    razorpay_order_id = models.CharField(max_length=64, unique=True)
    razorpay_payment_id = models.CharField(
        max_length=64, unique=True, null=True, blank=True
    )
    amount_paise = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(
        max_length=10, choices=OrderStatus.choices, default=OrderStatus.CREATED
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(course_pricing_plan__isnull=False, mock_test_pricing_plan__isnull=True)
                    | Q(course_pricing_plan__isnull=True, mock_test_pricing_plan__isnull=False)
                ),
                name="order_exactly_one_pricing_plan",
            ),
        ]

    def __str__(self):
        return self.razorpay_order_id


class Enrollment(models.Model):
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="enrollments"
    )
    course = models.ForeignKey(
        Course, on_delete=models.PROTECT, related_name="enrollments"
    )
    pricing_plan = models.ForeignKey(
        PricingPlan, on_delete=models.PROTECT, related_name="enrollments"
    )
    purchased_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    order = models.OneToOneField(
        Order, on_delete=models.PROTECT, related_name="enrollment"
    )

    def __str__(self):
        return f"{self.student} — {self.course}"


class MockTestEnrollment(models.Model):
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mock_test_enrollments",
    )
    mock_test = models.ForeignKey(
        MockTest, on_delete=models.PROTECT, related_name="enrollments"
    )
    purchased_at = models.DateTimeField(auto_now_add=True)
    order = models.OneToOneField(
        Order, on_delete=models.PROTECT, related_name="mock_test_enrollment"
    )

    def __str__(self):
        return f"{self.student} — {self.mock_test}"


class BlogPost(models.Model):
    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    body = models.TextField()
    published_at = models.DateTimeField(null=True, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="blog_posts"
    )

    class Meta:
        ordering = ["-published_at"]

    def __str__(self):
        return self.title
