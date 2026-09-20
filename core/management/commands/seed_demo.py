"""Seed coherent demo data across all LMS modules (mirrors the reference app).

Idempotent: re-running uses get_or_create on natural keys, so it won't create
duplicates. Run with:  python manage.py seed_demo
"""

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import LearnerStats, Role, TrainerProfile, UserProfile
from analytics.models import AnalyticsSnapshot
from assessments.models import (
    Answer,
    Assessment,
    Choice,
    Question,
    Rubric,
    Submission,
)
from certificates.models import Certificate, CertificateTemplate
from classrooms.models import (
    ClassroomSession,
    ContainerClassroom,
    Device,
    Room,
    Seat,
    SeatAttendance,
    SeatDeviceMapping,
    SmartEvent,
)
from core.models import Organization
from courses.models import (
    Batch,
    Category,
    Course,
    CourseReview,
    Enrollment,
    Lesson,
    LessonProgress,
    LessonResource,
    Module,
    Tag,
)
from engagement.models import (
    Badge,
    CommunityPost,
    CommunityProfile,
    DiscussionReply,
    DiscussionThread,
    UserBadge,
)
from library.models import LibraryBookmark, LibraryResource
from live.models import (
    Attendance,
    IndividualBooking,
    LiveSession,
    SessionDoubt,
    SessionRegistration,
    TrainerAvailability,
)
from notifications.models import (
    DeviceToken,
    Notification,
    NotificationCategory,
    NotificationPreference,
)
from payments.models import (
    Coupon,
    CoursePrice,
    Invoice,
    Order,
    OrderItem,
    Payment,
    PaymentMethod,
    PricingPlan,
    Refund,
    Subscription,
    TrainerPayout,
)
from recordings.models import (
    Recording,
    RecordingChapter,
    RecordingTranscript,
    RecordingView,
)

User = get_user_model()
PASSWORD = "Passw0rd!123"


def aware(y, m, d, hh=0, mm=0):
    return timezone.make_aware(datetime(y, m, d, hh, mm))


class Command(BaseCommand):
    help = "Seed demo data across all modules (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("Seeding demo data...")

        # ---- Users -------------------------------------------------------
        def make_user(email, name, role, **extra):
            u, created = User.objects.get_or_create(
                email=email, defaults={"full_name": name, "role": role, **extra}
            )
            if created:
                u.set_password(PASSWORD)
                u.save()
            return u

        org = Organization.objects.get_or_create(
            name="Acme Institute",
            defaults={"type": Organization.OrgType.INSTITUTION},
        )[0]

        admin = make_user(
            "admin@jigyasa.local", "Platform Admin", Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        kapoor = make_user("kapoor@jigyasa.local", "Dr. Kapoor", Role.TRAINER)
        riya = make_user("riya@jigyasa.local", "Riya Sharma", Role.STUDENT)
        ananya = make_user("ananya@jigyasa.local", "Ananya R.", Role.STUDENT)
        karan = make_user("karan@jigyasa.local", "Karan M.", Role.STUDENT)
        priya = make_user("priya@jigyasa.local", "Priya S.", Role.STUDENT)
        # The institution portal is the fourth role in ``Role`` and had no
        # seeded login, so there was no way to open it at all. Attached to the
        # org above because that is the only thing the role can administer.
        make_user(
            "institute@jigyasa.local", "Acme Institute Admin", Role.INSTITUTION,
            organization=org,
        )

        # ---- Profiles ----------------------------------------------------
        UserProfile.objects.get_or_create(
            user=riya,
            defaults={
                "headline": "Aspiring Data Scientist · learning in public",
                "bio": "Career-switcher into data. Working through Python, pandas, and visualisation — one project at a time.",
                "location": "Pune, India",
                "github_url": "https://github.com/riya",
                "linkedin_url": "https://linkedin.com/in/riya",
            },
        )
        TrainerProfile.objects.get_or_create(
            user=kapoor,
            defaults={
                "expertise": "Data Science, Python, React",
                "years_experience": 9,
                "rating_avg": 4.8,
                "rating_count": 326,
                "is_approved": True,
                # A negotiated rate, deliberately different from the platform
                # default, so the demo shows the override actually overriding.
                "revenue_share_pct": 75,
            },
        )
        LearnerStats.objects.get_or_create(
            user=riya,
            defaults={
                "streak_days": 7,
                "last_active_date": timezone.now().date(),
                "courses_enrolled": 3,
                "avg_progress": 53,
                "certificates_count": 3,
            },
        )

        # ---- Taxonomy ----------------------------------------------------
        cats = {
            slug: Category.objects.get_or_create(name=name)[0]
            for slug, name in [
                ("data", "Data Science"),
                ("design", "Design"),
                ("web", "Web Development"),
                ("finance", "Finance"),
                ("career", "Career"),
            ]
        }
        tags = {
            t: Tag.objects.get_or_create(name=t)[0]
            for t in ["python", "pandas", "sql", "data-viz", "react", "ux"]
        }
        riya.profile.tags.set([tags["python"], tags["pandas"], tags["sql"], tags["data-viz"]])

        # ---- Courses -----------------------------------------------------
        def make_course(title, cat, ctype, level, free, hours, subtitle=""):
            c, _ = Course.objects.get_or_create(
                title=title,
                defaults={
                    "trainer": kapoor,
                    "category": cats[cat],
                    "course_type": ctype,
                    "skill_level": level,
                    "is_free": free,
                    "subtitle": subtitle,
                    "description": f"{title} — a hands-on course by Dr. Kapoor.",
                    "duration_minutes": hours * 60,
                    "status": Course.Status.PUBLISHED,
                    "published_at": timezone.now(),
                    "rating_avg": 4.7,
                    "rating_count": 120,
                    "enrolled_count": 540,
                },
            )
            return c

        ds = make_course("Intro to Data Science", "data", Course.CourseType.SELF_PACED,
                         Course.SkillLevel.BEGINNER, True, 40, "Python, pandas & visualisation")
        ux = make_course("UX Fundamentals", "design", Course.CourseType.SELF_PACED,
                         Course.SkillLevel.BEGINNER, True, 18, "Design thinking & wireframing")
        fin = make_course("Financial Literacy 101", "finance", Course.CourseType.SELF_PACED,
                         Course.SkillLevel.BEGINNER, True, 24, "Money basics that compound")
        react = make_course("React 19 Pro", "web", Course.CourseType.LIVE_BATCH,
                         Course.SkillLevel.INTERMEDIATE, False, 30, "Modern React with Server Components")
        fullstack = make_course("Full-Stack Web Development", "web", Course.CourseType.LIVE_BATCH,
                         Course.SkillLevel.INTERMEDIATE, False, 80, "Cohort-based bootcamp")
        ds.tags.set([tags["python"], tags["pandas"], tags["data-viz"]])
        react.tags.set([tags["react"]])

        # Batch (Cohort 12) for the full-stack course
        cohort12 = Batch.objects.get_or_create(
            course=fullstack, name="Cohort 12",
            defaults={"trainer": kapoor, "organization": org, "capacity": 40,
                      "enrolled_count": 38,
                      "start_date": aware(2026, 5, 1).date(),
                      "end_date": aware(2026, 8, 1).date()},
        )[0]

        # CoursePrice for paid courses
        for c, amount in [(react, 4999), (fullstack, 6999)]:
            CoursePrice.objects.get_or_create(
                course=c, pricing_type=CoursePrice.PricingType.ONE_TIME,
                defaults={"amount": amount},
            )

        # ---- Curriculum for Intro to Data Science ------------------------
        curriculum = [
            ("01 · Foundations", [
                ("Welcome & how this course works", "video", 6),
                ("Installing Python & Jupyter", "video", 12),
                ("Your first notebook", "video", 14),
                ("Checkpoint quiz", "quiz", 10),
            ]),
            ("02 · Pandas in practice", [
                ("Series & DataFrames", "video", 16),
                ("Selecting with loc & iloc", "video", 18),
                ("Cleaning messy data", "video", 20),
                ("Group-by & aggregation", "video", 17),
                ("Practice assignment", "assignment", 45),
            ]),
            ("03 · Visualisation", [
                ("Plotting with matplotlib", "video", 15),
                ("Telling a visual story", "video", 14),
                ("Reading lesson: chart choices", "reading", 10),
            ]),
            ("04 · Capstone", [
                ("Brief & dataset", "reading", 8),
                ("Build your analysis", "assignment", 90),
                ("Final quiz", "quiz", 20),
            ]),
        ]
        ds_lessons = []
        for m_order, (mtitle, lessons) in enumerate(curriculum, start=1):
            module = Module.objects.get_or_create(
                course=ds, title=mtitle, defaults={"order": m_order}
            )[0]
            for l_order, (ltitle, ctype, dur) in enumerate(lessons, start=1):
                lesson = Lesson.objects.get_or_create(
                    module=module, title=ltitle,
                    defaults={"content_type": ctype, "order": l_order,
                              "duration_minutes": dur,
                              "is_preview": (m_order == 1 and l_order == 1)},
                )[0]
                ds_lessons.append(lesson)
        # a downloadable resource on the first lesson
        LessonResource.objects.get_or_create(
            lesson=ds_lessons[0], title="Course slides (PDF)",
            defaults={"url": "https://example.com/slides.pdf", "resource_type": "pdf"},
        )

        # ---- Assessments -------------------------------------------------
        # No ``total_questions`` here on purpose: it is derived from the
        # question rows (assessments.signals). Seeding it by hand is how
        # "Final quiz" ended up claiming 2 questions it never had.
        checkpoint = Assessment.objects.get_or_create(
            course=ds, title="Foundations checkpoint",
            defaults={"trainer": kapoor, "assessment_type": Assessment.AssessmentType.QUIZ,
                      "time_limit_minutes": 10, "pass_percent": 70, "max_attempts": 3,
                      "is_published": True,
                      "lesson": ds_lessons[3]},
        )[0]
        final_quiz = Assessment.objects.get_or_create(
            course=ds, title="Final quiz",
            defaults={"trainer": kapoor, "assessment_type": Assessment.AssessmentType.QUIZ,
                      "time_limit_minutes": 20, "pass_percent": 60, "max_attempts": 2,
                      "is_published": True,
                      "lesson": ds_lessons[-1]},
        )[0]
        practice = Assessment.objects.get_or_create(
            course=ds, title="Practice assignment",
            defaults={"trainer": kapoor, "assessment_type": Assessment.AssessmentType.ASSIGNMENT,
                      "grading_type": Assessment.GradingType.RUBRIC, "pass_percent": 50,
                      "is_published": True, "lesson": ds_lessons[8]},
        )[0]
        Rubric.objects.get_or_create(
            assessment=practice,
            defaults={"criteria": [{"name": "Correctness", "max_points": 50},
                                   {"name": "Clarity", "max_points": 50}]},
        )

        # Questions + choices for the checkpoint
        q_specs = [
            ("Which library is used for DataFrames?", [("pandas", True), ("requests", False), ("flask", False)]),
            ("What does .iloc select by?", [("Integer position", True), ("Label", False), ("Both", False)]),
            ("Jupyter notebooks have the extension?", [(".ipynb", True), (".py", False), (".js", False)]),
        ]
        for qi, (qtext, choices) in enumerate(q_specs, start=1):
            q = Question.objects.get_or_create(
                assessment=checkpoint, text=qtext,
                defaults={"order": qi, "points": 1},
            )[0]
            for ci, (ctext, correct) in enumerate(choices, start=1):
                Choice.objects.get_or_create(
                    question=q, text=ctext,
                    defaults={"is_correct": correct, "order": ci},
                )

        # ---- Enrollments + progress -------------------------------------
        def enroll(student, course, pct, status=Enrollment.Status.ACTIVE, completed=False):
            e, _ = Enrollment.objects.get_or_create(
                student=student, course=course,
                defaults={"status": status, "source": Enrollment.Source.FREE,
                          "progress_pct": pct,
                          "completed_at": timezone.now() if completed else None},
            )
            return e

        ds_enroll = enroll(riya, ds, 42)
        enroll(riya, ux, 18)
        enroll(riya, fin, 100, Enrollment.Status.COMPLETED, completed=True)
        enroll(ananya, ds, 30)
        enroll(karan, react, 55)
        enroll(priya, ux, 12)

        # Mark the first 6 DS lessons complete for Riya (≈42%)
        for lesson in ds_lessons[:6]:
            LessonProgress.objects.get_or_create(
                enrollment=ds_enroll, lesson=lesson,
                defaults={"status": LessonProgress.Status.COMPLETED, "watch_pct": 100,
                          "time_spent_seconds": lesson.duration_minutes * 60,
                          "completed_at": timezone.now()},
            )

        CourseReview.objects.get_or_create(
            course=ds, student=riya,
            defaults={"rating": 5, "comment": "Clear and practical. Loved the pandas section."},
        )

        # Riya passed the Foundations checkpoint 100%
        sub = Submission.objects.get_or_create(
            assessment=checkpoint, student=riya, attempt_no=1,
            defaults={"enrollment": ds_enroll, "status": Submission.Status.PASSED,
                      "started_at": timezone.now() - timedelta(minutes=8),
                      "submitted_at": timezone.now(), "score": 3, "percent": 100,
                      "passed": True, "time_taken_seconds": 480},
        )[0]
        for q in checkpoint.questions.all():
            ans = Answer.objects.get_or_create(
                submission=sub, question=q,
                defaults={"is_correct": True, "points_awarded": 1},
            )[0]
            correct_choice = q.choices.filter(is_correct=True).first()
            if correct_choice:
                ans.selected_choices.set([correct_choice])

        # ---- Certificates ------------------------------------------------
        template = CertificateTemplate.objects.get_or_create(name="Default certificate")[0]
        cert_specs = [
            (fin, "JGY-2025-001284", "A", 24, aware(2026, 1, 15).date()),
            (ds, "JGY-2026-004217", "Distinction", 40, aware(2026, 3, 8).date()),
            (react, "JGY-2026-009630", "A", 24, aware(2026, 5, 22).date()),
        ]
        for course, serial, grade, hours, issued in cert_specs:
            Certificate.objects.get_or_create(
                serial_number=serial,
                defaults={"student": riya, "course": course, "template": template,
                          "grade": grade, "total_hours": hours, "issued_date": issued,
                          "verification_code": serial.replace("JGY-", "VC").replace("-", ""),
                          "status": Certificate.Status.ISSUED},
            )

        # ---- Payments / billing -----------------------------------------
        pro = PricingPlan.objects.get_or_create(
            slug="pro",
            defaults={"name": "Pro", "billing_period": PricingPlan.BillingPeriod.MONTHLY,
                      "price": 499, "is_active": True,
                      "features": ["All paid courses", "Live cohorts", "Certificates",
                                   "Priority support"]},
        )[0]
        pm = PaymentMethod.objects.get_or_create(
            user=riya, brand="Visa", last4="4242",
            defaults={"type": PaymentMethod.MethodType.CARD, "expiry": "09/27",
                      "is_default": True},
        )[0]
        Subscription.objects.get_or_create(
            user=riya, plan=pro,
            defaults={"status": Subscription.Status.ACTIVE, "payment_method": pm,
                      "current_period_start": aware(2026, 6, 1),
                      "current_period_end": aware(2026, 7, 1)},
        )
        Coupon.objects.get_or_create(
            code="WELCOME10",
            defaults={"discount_type": Coupon.DiscountType.PERCENT, "value": 10,
                      "scope": Coupon.Scope.ALL, "is_active": True},
        )

        invoice_specs = [
            ("JIG-2026-0512", "Pro plan — June 2026", 499, 90, "paid", aware(2026, 6, 1).date()),
            ("JIG-2026-0488", "Full-Stack Web Development (Cohort 12)", 6999, 1260, "paid", aware(2026, 5, 18).date()),
            ("JIG-2026-0455", "Pro plan — May 2026", 499, 90, "paid", aware(2026, 5, 1).date()),
            ("JIG-2026-0431", "Data Structures & Algorithms Intensive", 4499, 810, "pending", aware(2026, 4, 22).date()),
            ("JIG-2026-0399", "UI/UX Design Foundations", 3299, 594, "refunded", aware(2026, 3, 30).date()),
        ]
        first_paid_payment = None
        refunded_payment = None
        for number, desc, amount, gst, status, issued in invoice_specs:
            order_status = {
                "paid": Order.Status.PAID, "pending": Order.Status.PENDING,
                "refunded": Order.Status.REFUNDED,
            }[status]
            order = Order.objects.get_or_create(
                user=riya, total=amount + gst,
                defaults={"status": order_status, "subtotal": amount, "tax_gst": gst},
            )[0]
            OrderItem.objects.get_or_create(
                order=order, title=desc,
                defaults={"item_type": OrderItem.ItemType.COURSE, "amount": amount},
            )
            Invoice.objects.get_or_create(
                number=number,
                defaults={"order": order, "user": riya, "description": desc,
                          "amount": amount, "gst_amount": gst,
                          "status": status, "issued_date": issued},
            )
            if status in ("paid", "refunded"):
                pay = Payment.objects.get_or_create(
                    order=order, gateway=Payment.Gateway.RAZORPAY,
                    defaults={"gateway_payment_id": f"pay_{number}", "amount": amount + gst,
                              "method": "card", "status": Payment.Status.SUCCESS,
                              "paid_at": timezone.now()},
                )[0]
                if status == "paid" and first_paid_payment is None:
                    first_paid_payment = pay
                if status == "refunded":
                    refunded_payment = pay
        if refunded_payment:
            Refund.objects.get_or_create(
                payment=refunded_payment,
                defaults={"amount": refunded_payment.amount, "reason": "Course withdrawn",
                          "status": Refund.Status.PROCESSED, "processed_at": timezone.now()},
            )
        TrainerPayout.objects.get_or_create(
            trainer=kapoor, period_start=aware(2026, 5, 1).date(),
            defaults={"period_end": aware(2026, 5, 31).date(), "gross": 50000,
                      "platform_fee": 15000, "net": 35000,
                      "status": TrainerPayout.Status.PAID, "paid_at": timezone.now()},
        )

        # ---- Live sessions ----------------------------------------------
        live_specs = [
            ("Server Components — Q&A", aware(2026, 6, 18, 19, 0), 60, 124, react),
            ("Pandas patterns workshop", aware(2026, 6, 20, 17, 0), 90, 86, ds),
            ("Speaking circle: tone & pace", aware(2026, 6, 22, 20, 0), 60, 52, ux),
        ]
        sessions = []
        for title, start, dur, attending, course in live_specs:
            s = LiveSession.objects.get_or_create(
                title=title,
                defaults={"trainer": kapoor, "course": course,
                          "session_type": LiveSession.SessionType.GROUP,
                          "scheduled_start": start, "duration_minutes": dur,
                          "capacity": 200, "registration_limit": 200,
                          "status": LiveSession.Status.SCHEDULED,
                          "attendees_count": attending,
                          "join_url": "https://meet.example.com/" + title.split()[0].lower()},
            )[0]
            sessions.append(s)
        SessionRegistration.objects.get_or_create(
            session=sessions[1], student=riya,
            defaults={"status": SessionRegistration.Status.REGISTERED},
        )
        Attendance.objects.get_or_create(
            session=sessions[0], student=karan,
            defaults={"present": True, "duration_seconds": 3500},
        )
        SessionDoubt.objects.get_or_create(
            session=sessions[1], student=riya,
            defaults={"text": "How do I reshape with melt vs pivot?",
                      "status": SessionDoubt.Status.OPEN},
        )
        slot = TrainerAvailability.objects.get_or_create(
            trainer=kapoor, start=aware(2026, 6, 25, 11, 0),
            defaults={"end": aware(2026, 6, 25, 12, 0), "slot_minutes": 60, "is_booked": True},
        )[0]
        IndividualBooking.objects.get_or_create(
            trainer=kapoor, student=riya, start=slot.start,
            defaults={"duration_minutes": 60, "status": IndividualBooking.Status.CONFIRMED,
                      "meeting_url": "https://meet.example.com/1on1-riya"},
        )

        # ---- Recordings --------------------------------------------------
        rec_specs = [
            ("Pandas Patterns — Cleaning & Reshaping Data", ds, aware(2026, 6, 15).date(), 72 * 60, 214),
            ("React Server Components — Deep Dive Q&A", react, aware(2026, 6, 12).date(), 58 * 60, 187),
        ]
        recs = []
        for title, course, rdate, dur, views in rec_specs:
            r = Recording.objects.get_or_create(
                title=title,
                defaults={"course": course, "trainer": kapoor, "recorded_date": rdate,
                          "duration_seconds": dur, "views_count": views,
                          "status": Recording.Status.READY,
                          "ai_summary": "Auto-generated summary of the session.",
                          "video_url": "https://cdn.example.com/rec.mp4"},
            )[0]
            recs.append(r)
        RecordingChapter.objects.get_or_create(
            recording=recs[0], title="Intro", defaults={"start_seconds": 0, "order": 1})
        RecordingChapter.objects.get_or_create(
            recording=recs[0], title="Handling NaNs", defaults={"start_seconds": 600, "order": 2})
        RecordingTranscript.objects.get_or_create(
            recording=recs[0],
            defaults={"segments": [{"start": 0, "end": 12, "text": "Welcome back everyone."}]})
        RecordingView.objects.get_or_create(
            recording=recs[0], user=riya,
            defaults={"watched_seconds": 4320, "last_position": 4320, "completed": True})

        # ---- Library -----------------------------------------------------
        lib_specs = [
            ("Linear Regression from Scratch", "video", "data", "free", 42, 0, 4800),
            ("Acing the Technical Interview", "webinar", "career", "free", 64, 0, 3500),
            ("The Pandas Field Guide", "ebook", "data", "premium", 0, 184, 3100),
            ("React Server Components, Demystified", "video", "web", "free", 58, 0, 2700),
            ("Foundations of Visual Hierarchy", "video", "design", "free", 36, 0, 2200),
            ("Deploying Next.js to the Edge", "video", "web", "free", 49, 0, 2100),
        ]
        lib = {}
        for title, fmt, cat, access, mins, pages, views in lib_specs:
            res = LibraryResource.objects.get_or_create(
                title=title,
                defaults={"format": fmt, "category": cats[cat], "author": kapoor,
                          "access_level": access, "duration_minutes": mins, "pages": pages,
                          "views_count": views, "popularity_score": views,
                          "published_at": timezone.now(),
                          "description": f"{title} — curated library resource."},
            )[0]
            lib[title] = res
        LibraryBookmark.objects.get_or_create(user=riya, resource=lib["The Pandas Field Guide"])

        # ---- Discussions -------------------------------------------------
        thread_specs = [
            (ds, ananya, "What is the difference between loc and iloc?",
             DiscussionThread.Status.RESOLVED, kapoor,
             "loc selects by label, iloc by integer position."),
            (react, karan, "useEffect cleanup function not running on unmount?",
             DiscussionThread.Status.OPEN, kapoor,
             "Make sure you return the cleanup from the effect callback."),
            (ux, priya, "Best tools for wireframing in 2025?",
             DiscussionThread.Status.OPEN, kapoor,
             "Figma remains the default; try Penpot if you want open-source."),
        ]
        for course, author, title, status, replier, reply_body in thread_specs:
            thread = DiscussionThread.objects.get_or_create(
                course=course, author=author, title=title,
                defaults={"status": status, "scope": DiscussionThread.Scope.COURSE,
                          "body": "Could someone clarify this?", "reply_count": 1,
                          "last_activity_at": timezone.now()},
            )[0]
            DiscussionReply.objects.get_or_create(
                thread=thread, author=replier, body=reply_body,
                defaults={"is_accepted_answer": status == DiscussionThread.Status.RESOLVED},
            )

        # ---- Community / gamification ------------------------------------
        CommunityProfile.objects.get_or_create(
            user=riya, defaults={"points": 1240, "level": 4, "badges_count": 3})
        badge_specs = [
            ("first-steps", "First Steps", "🚀"),
            ("quiz-master", "Quiz Master", "🧠"),
            ("seven-day-streak", "7-Day Streak", "🔥"),
            ("top-contributor", "Top Contributor", "🏅"),
        ]
        badges = []
        for slug, name, icon in badge_specs:
            b = Badge.objects.get_or_create(
                slug=slug, defaults={"name": name, "icon": icon,
                                     "description": f"Awarded for: {name}."})[0]
            badges.append(b)
        for b in badges[:3]:
            UserBadge.objects.get_or_create(user=riya, badge=b)
        CommunityPost.objects.get_or_create(
            author=riya, body="Just finished the pandas module — loc/iloc finally clicks!",
            defaults={"post_type": "win", "likes_count": 12})

        # ---- Notifications ----------------------------------------------
        notif_specs = [
            (NotificationCategory.LIVE_CLASS, "Pandas patterns workshop starts soon",
             "Your live class begins at 5:00 PM."),
            (NotificationCategory.CERTIFICATE, "Certificate issued",
             "Your Intro to Data Science certificate is ready."),
            (NotificationCategory.ASSESSMENT, "New assessment posted",
             "Final quiz is now available."),
        ]
        for cat, title, body in notif_specs:
            Notification.objects.get_or_create(
                recipient=riya, title=title,
                defaults={"category": cat, "body": body, "is_read": False})

        pref_matrix = {
            NotificationCategory.COURSE:      (True, True, False, False, True),
            NotificationCategory.LIVE_CLASS:  (True, True, True, True, True),
            NotificationCategory.ASSESSMENT:  (True, True, False, False, True),
            NotificationCategory.CERTIFICATE: (True, True, False, False, False),
            NotificationCategory.FORUM:       (True, False, False, False, True),
            NotificationCategory.PAYMENT:     (True, True, True, False, False),
            NotificationCategory.SYSTEM:      (True, True, False, False, False),
        }
        for cat, (in_app, email, sms, wa, push) in pref_matrix.items():
            NotificationPreference.objects.get_or_create(
                user=riya, category=cat,
                defaults={"in_app": in_app, "email": email, "sms": sms,
                          "whatsapp": wa, "push": push})
        DeviceToken.objects.get_or_create(
            user=riya, token="demo-web-token-123",
            defaults={"platform": DeviceToken.Platform.WEB})

        # ---- Classrooms (design-ready, minimal sample) ------------------
        room = Room.objects.get_or_create(
            organization=org, name="Smart Room 1",
            defaults={"room_type": Room.RoomType.SMART, "capacity": 30,
                      "location": "Pune Campus"})[0]
        seat_a1 = Seat.objects.get_or_create(room=room, label="A1", defaults={"row": 1, "col": 1})[0]
        seat_a2 = Seat.objects.get_or_create(room=room, label="A2", defaults={"row": 1, "col": 2})[0]
        mic1 = Device.objects.get_or_create(
            organization=org, identifier="Mic ID 001",
            defaults={"device_type": Device.DeviceType.MIC, "status": "online"})[0]
        cam1 = Device.objects.get_or_create(
            organization=org, identifier="PTZ Cam 001",
            defaults={"device_type": Device.DeviceType.CAMERA, "status": "online"})[0]
        SeatDeviceMapping.objects.get_or_create(seat=seat_a1, device=mic1)
        SeatDeviceMapping.objects.get_or_create(seat=seat_a2, device=cam1)
        csession = ClassroomSession.objects.get_or_create(
            room=room, live_session=sessions[0],
            defaults={"remote_trainer": kapoor, "date": sessions[0].scheduled_start,
                      "status": ClassroomSession.Status.SCHEDULED})[0]
        SeatAttendance.objects.get_or_create(
            classroom_session=csession, seat=seat_a1,
            defaults={"student": riya, "present": True})
        SmartEvent.objects.get_or_create(
            classroom_session=csession, seat=seat_a1,
            event_type=SmartEvent.EventType.RAISE_HAND,
            defaults={"payload": {"row": 1}})
        container_room = Room.objects.get_or_create(
            organization=org, name="Mobile Unit 7",
            defaults={"room_type": Room.RoomType.CONTAINER, "capacity": 20})[0]
        ContainerClassroom.objects.get_or_create(
            room=container_room,
            defaults={"gps": "18.5204,73.8567", "connectivity_status": "online",
                      "power_status": "battery 82%", "mobile_unit_id": "MU-07"})

        # ---- The institution console (PRD §2.4) --------------------------
        # Its own learners, deliberately not the four demo students above.
        # Attaching those to an organisation would pull their threads out of
        # the orgless public pool that the student community screens read from
        # (see ``engagement.views.visible_threads``) — seeding the institution
        # must not quietly empty a different demo screen.
        #
        # Dated **relative to today**, unlike the fixed 2026 dates elsewhere in
        # this file. The console's headline tile counts batches whose window
        # contains today, so fixed dates would make it read zero a few months
        # after they were written — which is exactly what the hard-coded
        # sample screen it replaces already did.
        today = timezone.localdate()
        inst_learners = [
            make_user(f"{handle}@acme.local", name, Role.STUDENT,
                      organization=org)
            for handle, name in [
                ("meera", "Meera Iyer"), ("arjun", "Arjun Nair"),
                ("fatima", "Fatima Sheikh"), ("dev", "Dev Malhotra"),
                ("sana", "Sana Qureshi"), ("rohit", "Rohit Deshmukh"),
            ]
        ]

        inst_batches = []
        for name, course_obj, offset_start, offset_end, cap in [
            ("Batch A · CS 2025", react, -21, 45, 40),
            ("Batch B · DS 2025", ds, -60, 10, 30),
            ("Batch C · Lang 2025", ux, 14, 90, 25),
            ("Batch Z · 2024 (closed)", fullstack, -365, -200, 20),
        ]:
            b = Batch.objects.get_or_create(
                course=course_obj, name=name,
                defaults={"trainer": kapoor, "organization": org,
                          "capacity": cap,
                          "start_date": today + timedelta(days=offset_start),
                          "end_date": today + timedelta(days=offset_end)},
            )[0]
            inst_batches.append(b)

        # Spread the learners across the three live batches with a range of
        # progress, so "avg. completion" is a real average rather than one
        # number repeated. ``Enrollment`` is unique per (student, course), so
        # each learner takes a different course.
        for learner, batch, pct in [
            (inst_learners[0], inst_batches[0], 72),
            (inst_learners[1], inst_batches[0], 64),
            (inst_learners[2], inst_batches[1], 45),
            (inst_learners[3], inst_batches[1], 38),
            (inst_learners[4], inst_batches[2], 91),
            (inst_learners[5], inst_batches[2], 12),
        ]:
            Enrollment.objects.get_or_create(
                student=learner, course=batch.course,
                defaults={"status": Enrollment.Status.ACTIVE,
                          "source": Enrollment.Source.INSTITUTION,
                          "progress_pct": pct, "batch": batch},
            )
        for batch in inst_batches[:3]:
            Batch.objects.filter(pk=batch.pk).update(
                enrolled_count=batch.enrollments.count()
            )

        # An upcoming room booking, so the bookings panel is not empty. Also
        # relative to today, for the same reason the batches are.
        lecture_hall = Room.objects.get_or_create(
            organization=org, name="Room 101",
            defaults={"room_type": Room.RoomType.PHYSICAL, "capacity": 60,
                      "location": "Pune Campus · Block A"})[0]
        for title, batch, in_days, hour, dur in [
            ("Batch A · live lecture", inst_batches[0], 3, 9, 180),
            ("Batch B · lab session", inst_batches[1], 6, 14, 180),
        ]:
            ls = LiveSession.objects.get_or_create(
                title=title,
                defaults={"trainer": kapoor, "course": batch.course,
                          "batch": batch,
                          "session_type": LiveSession.SessionType.GROUP,
                          "scheduled_start": timezone.make_aware(
                              datetime.combine(
                                  today + timedelta(days=in_days),
                                  datetime.min.time().replace(hour=hour))),
                          "duration_minutes": dur, "capacity": 60,
                          "status": LiveSession.Status.SCHEDULED},
            )[0]
            ClassroomSession.objects.get_or_create(
                room=lecture_hall, live_session=ls,
                defaults={"remote_trainer": kapoor,
                          "date": ls.scheduled_start,
                          "status": ClassroomSession.Status.SCHEDULED},
            )

        # ---- Analytics snapshot -----------------------------------------
        AnalyticsSnapshot.objects.get_or_create(
            scope=AnalyticsSnapshot.Scope.TRAINER, owner=kapoor,
            period_start=aware(2026, 6, 1).date(),
            defaults={"period_end": aware(2026, 6, 30).date(),
                      "metrics": {"active_students": 540, "completion_pct": 61,
                                  "avg_rating": 4.8, "doubt_count": 23}})

        self.stdout.write(self.style.SUCCESS("Demo data seeded successfully."))
        self.stdout.write(
            f"  Users: {User.objects.count()} | Courses: {Course.objects.count()} | "
            f"Lessons: {Lesson.objects.count()} | Enrollments: {Enrollment.objects.count()} | "
            f"Invoices: {Invoice.objects.count()} | Certificates: {Certificate.objects.count()}"
        )
        self.stdout.write(f"  Demo login password for all seeded users: {PASSWORD}")
