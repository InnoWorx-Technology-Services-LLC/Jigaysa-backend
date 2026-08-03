"""Certificate issuance (PRD §3.12 — auto certificate generation).

Kept separate from views so issuance can be triggered both by the API
(student claim / admin) and automatically when an enrollment is completed
(called from the courses progress recompute via a lazy import).
"""

import secrets
from datetime import date

from django.db import IntegrityError

from certificates.models import Certificate, CertificateTemplate


def _gen_serial():
    return f"JGY-{date.today().year}-{secrets.token_hex(3).upper()}"


def issue_for_enrollment(enrollment, *, force=False):
    """Idempotently issue a completion certificate for a completed enrollment.

    Returns ``(certificate, created)``. If the enrollment is not completed and
    ``force`` is False, returns ``(None, False)``. If an issued certificate
    already exists for this student+course, that one is returned unchanged.
    """
    from courses.models import Enrollment

    if not force and enrollment.status != Enrollment.Status.COMPLETED:
        return None, False

    # The trainer's "Certificate of completion" toggle. ``force`` still wins so
    # an admin can issue one by hand for a course that has it switched off.
    if not force and not enrollment.course.certificate_enabled:
        return None, False

    existing = Certificate.objects.filter(
        student=enrollment.student,
        course=enrollment.course,
        status=Certificate.Status.ISSUED,
    ).first()
    if existing:
        return existing, False

    template = CertificateTemplate.objects.filter(
        course=enrollment.course
    ).first()
    total_hours = round((enrollment.course.duration_minutes or 0) / 60)

    # serial_number / verification_code are unique; retry on the rare clash.
    for _ in range(5):
        try:
            cert = Certificate.objects.create(
                student=enrollment.student,
                course=enrollment.course,
                enrollment=enrollment,
                template=template,
                serial_number=_gen_serial(),
                verification_code=secrets.token_urlsafe(16),
                issued_date=date.today(),
                total_hours=total_hours,
            )
            return cert, True
        except IntegrityError:
            continue
    raise RuntimeError("Could not generate a unique certificate serial.")
