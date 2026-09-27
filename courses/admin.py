from django.contrib import admin

from courses.models import (
    Batch,
    Category,
    Course,
    CourseReview,
    Enrollment,
    FeedbackAnswer,
    FeedbackForm,
    FeedbackQuestion,
    FeedbackResponse,
    Lesson,
    LessonProgress,
    LessonResource,
    Module,
    Tag,
)


class ModuleInline(admin.TabularInline):
    model = Module
    extra = 0


class LessonInline(admin.TabularInline):
    model = Lesson
    extra = 0


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "parent")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "trainer",
        "course_type",
        "status",
        "is_free",
        "rating_avg",
        "enrolled_count",
    )
    list_filter = ("status", "course_type", "skill_level", "is_free", "visibility")
    search_fields = ("title", "subtitle", "trainer__email")
    autocomplete_fields = ("trainer", "category")
    inlines = [ModuleInline]


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ("title", "course", "order")
    search_fields = ("title", "course__title")
    inlines = [LessonInline]


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ("title", "module", "content_type", "order", "is_preview")
    list_filter = ("content_type", "is_preview")
    search_fields = ("title",)


@admin.register(LessonResource)
class LessonResourceAdmin(admin.ModelAdmin):
    list_display = ("title", "lesson", "resource_type")


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("name", "course", "trainer", "start_date", "capacity")
    search_fields = ("name", "course__title")


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ("student", "course", "status", "source", "progress_pct")
    list_filter = ("status", "source")
    search_fields = ("student__email", "course__title")


@admin.register(LessonProgress)
class LessonProgressAdmin(admin.ModelAdmin):
    list_display = ("enrollment", "lesson", "status", "watch_pct")
    list_filter = ("status",)


@admin.register(CourseReview)
class CourseReviewAdmin(admin.ModelAdmin):
    list_display = ("course", "student", "rating", "created_at")
    list_filter = ("rating",)


class FeedbackQuestionInline(admin.TabularInline):
    model = FeedbackQuestion
    extra = 0


class FeedbackAnswerInline(admin.TabularInline):
    model = FeedbackAnswer
    extra = 0


@admin.register(FeedbackForm)
class FeedbackFormAdmin(admin.ModelAdmin):
    list_display = ("title", "course", "is_active", "is_anonymous")
    list_filter = ("is_active", "is_anonymous", "require_completion")
    search_fields = ("title", "course__title")
    inlines = [FeedbackQuestionInline]


@admin.register(FeedbackResponse)
class FeedbackResponseAdmin(admin.ModelAdmin):
    list_display = ("form", "student", "submitted_at")
    search_fields = ("student__email", "form__course__title")
    inlines = [FeedbackAnswerInline]
