from django.contrib import admin
from django.urls import include, path

from core import student_views, views

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
    path("my-mock-tests/", student_views.my_mock_tests, name="my_mock_tests"),
    path("profile/", student_views.profile, name="profile"),
]
