import logging

from django.contrib import admin, messages
from django.http import HttpResponseRedirect

from core.forms import ChapterAdminForm
from core.models import (
    Attempt,
    AttemptAnswer,
    Book,
    BlogPost,
    Chapter,
    Course,
    CourseMockTest,
    Enrollment,
    MockTest,
    MockTestEnrollment,
    MockTestPricingPlan,
    Order,
    PricingPlan,
    Question,
    UserChapterProgress,
    UserProfile,
)
from core.r2 import R2Error, upload_chapter_pdf

logger = logging.getLogger(__name__)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "phone_number")
    search_fields = ("user__username", "user__email", "phone_number")


class PricingPlanInline(admin.TabularInline):
    model = PricingPlan
    extra = 0


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("title", "is_published")
    list_filter = ("is_published",)
    search_fields = ("title",)
    inlines = [PricingPlanInline]


@admin.register(PricingPlan)
class PricingPlanAdmin(admin.ModelAdmin):
    list_display = ("course", "duration_months", "price_paise")
    list_filter = ("duration_months",)
    search_fields = ("course__title",)


class ChapterInline(admin.TabularInline):
    model = Chapter
    extra = 0
    # The raw PDF key is not editable; PDFs are uploaded on the Chapter form.
    fields = ("title", "order")


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ("course", "title", "order")
    list_filter = ("course",)
    search_fields = ("title", "course__title")
    ordering = ("course", "order")
    inlines = [ChapterInline]


@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    form = ChapterAdminForm
    list_display = ("book", "title", "order", "has_pdf")
    list_filter = ("book__course",)
    search_fields = ("title", "book__title")
    ordering = ("book", "order")
    readonly_fields = ("has_pdf",)

    @admin.display(boolean=True, description="PDF uploaded")
    def has_pdf(self, obj):
        return bool(obj and obj.pdf_object_key)

    def save_model(self, request, obj, form, change):
        """API.md §7: after the row exists, upload the PDF server-side and store only its key."""
        super().save_model(request, obj, form, change)
        upload = form.cleaned_data.get("pdf_file")
        if upload:
            obj.pdf_object_key = upload_chapter_pdf(obj.pk, upload)
            obj.save(update_fields=["pdf_object_key"])

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # The admin saves inside one transaction, so an R2 failure raised from
        # save_model rolls the whole save back. Report it instead of a false success.
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except R2Error as exc:
            logger.error("Chapter PDF upload failed: %s", exc)
            self.message_user(
                request,
                "The PDF could not be uploaded to storage, so nothing was saved. Please try again.",
                messages.ERROR,
            )
            return HttpResponseRedirect(request.get_full_path())


@admin.register(UserChapterProgress)
class UserChapterProgressAdmin(admin.ModelAdmin):
    list_display = ("user", "chapter", "is_completed", "last_accessed_at")
    list_filter = ("is_completed",)
    search_fields = ("user__username", "chapter__title")


@admin.register(MockTest)
class MockTestAdmin(admin.ModelAdmin):
    list_display = ("title", "duration_minutes", "is_published")
    list_filter = ("is_published",)
    search_fields = ("title",)


@admin.register(MockTestPricingPlan)
class MockTestPricingPlanAdmin(admin.ModelAdmin):
    list_display = ("mock_test", "price_paise")
    search_fields = ("mock_test__title",)


@admin.register(CourseMockTest)
class CourseMockTestAdmin(admin.ModelAdmin):
    list_display = ("course", "mock_test", "order")
    list_filter = ("course",)
    search_fields = ("course__title", "mock_test__title")
    ordering = ("course", "order")


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "mock_test", "chapter", "correct_option")
    list_filter = ("mock_test", "chapter", "correct_option")
    search_fields = ("text",)


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ("student", "mock_test", "started_at", "submitted_at", "score")
    list_filter = ("mock_test",)
    search_fields = ("student__username",)


@admin.register(AttemptAnswer)
class AttemptAnswerAdmin(admin.ModelAdmin):
    list_display = ("attempt", "question", "selected_option", "saved_at")
    search_fields = ("attempt__student__username",)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "razorpay_order_id",
        "student",
        "status",
        "amount_paise",
        "currency",
        "verified_at",
    )
    list_filter = ("status", "currency")
    search_fields = ("razorpay_order_id", "razorpay_payment_id", "student__username")


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ("student", "course", "purchased_at", "expires_at")
    list_filter = ("course",)
    search_fields = ("student__username", "course__title")


@admin.register(MockTestEnrollment)
class MockTestEnrollmentAdmin(admin.ModelAdmin):
    list_display = ("student", "mock_test", "purchased_at")
    list_filter = ("mock_test",)
    search_fields = ("student__username", "mock_test__title")


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "published_at")
    list_filter = ("author",)
    search_fields = ("title", "body")
    prepopulated_fields = {"slug": ("title",)}
