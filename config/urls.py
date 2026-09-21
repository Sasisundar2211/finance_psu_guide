from django.contrib import admin
from django.urls import include, path

from core import mock_views, payment_views, practice_views, student_views, views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", views.home, name="home"),
    path("courses/", views.course_list, name="course_list"),
    path("courses/<int:pk>/", views.course_detail, name="course_detail"),
    path("mock-tests/", views.mock_test_list, name="mock_test_list"),
    path("mock-tests/<int:pk>/", views.mock_test_detail, name="mock_test_detail"),
    path("blog/", views.blog_list, name="blog_list"),
    path("blog/<slug:slug>/", views.blog_detail, name="blog_detail"),
    path("dashboard/", student_views.dashboard, name="dashboard"),
    path("library/", student_views.my_courses, name="my_courses"),
    path("library/course/<int:pk>/", student_views.course_library, name="course_library"),
    path(
        "library/chapter/<int:chapter_id>/",
        student_views.chapter_viewer,
        name="chapter_viewer",
    ),
    path(
        "library/chapter/<int:chapter_id>/signed-url/",
        student_views.chapter_signed_url,
        name="chapter_signed_url",
    ),
    path(
        "library/chapter/<int:chapter_id>/practice/",
        practice_views.practice_page,
        name="practice_page",
    ),
    path(
        "course/chapter/<int:chapter_id>/practice-mcqs/",
        practice_views.practice_mcqs,
        name="practice_mcqs",
    ),
    path(
        "course/chapter/<int:chapter_id>/practice-mcqs/<int:question_id>/check/",
        practice_views.practice_check,
        name="practice_check",
    ),
    path("mocktest/<int:mock_test_id>/start/", mock_views.start, name="mock_test_start"),
    path("mocktest/attempt/<int:attempt_id>/", mock_views.attempt_page, name="attempt_page"),
    path(
        "mocktest/attempt/<int:attempt_id>/autosave/",
        mock_views.autosave,
        name="attempt_autosave",
    ),
    path(
        "mocktest/attempt/<int:attempt_id>/submit/",
        mock_views.submit,
        name="attempt_submit",
    ),
    path(
        "checkout/create-order/",
        payment_views.create_order,
        name="checkout_create_order",
    ),
    path(
        "checkout/order/<str:razorpay_order_id>/status/",
        payment_views.order_status,
        name="checkout_order_status",
    ),
    path("checkout/webhook/", payment_views.razorpay_webhook, name="checkout_webhook"),
    path("my-mock-tests/", student_views.my_mock_tests, name="my_mock_tests"),
    path("profile/", student_views.profile, name="profile"),
]
