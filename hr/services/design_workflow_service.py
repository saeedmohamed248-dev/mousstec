"""
Service: Design Approval Workflow

Responsibilities:
- Handle design submission with auto-approve logic
- Route to supervisor for review when auto-approve is disabled
- Process approval / rejection by supervisor
- Handle revision requests
"""

import logging

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


class DesignWorkflowService:
    """All design approval workflow operations go through here."""

    # ------------------------------------------------------------------
    # Submit Design — designer uploads work
    # ------------------------------------------------------------------
    @staticmethod
    def submit_design(designer_employee, title, design_file, execution_type='manual',
                      description='', preview_image=None, related_order_id=None):
        """
        Submit a new design.
        If designer has auto_approve_designs enabled -> auto-approve immediately.
        Otherwise -> route to supervisor for review.
        Returns the DesignSubmission instance.
        """
        from hr.models import DesignSubmission

        with transaction.atomic():
            submission = DesignSubmission(
                designer=designer_employee,
                title=title,
                description=description,
                design_file=design_file,
                execution_type=execution_type,
                related_order_id=related_order_id,
            )
            if preview_image:
                submission.preview_image = preview_image

            # --- Auto-approve check ---
            if designer_employee.auto_approve_designs:
                submission.status = 'approved'
                submission.auto_approved = True
                submission.reviewed_at = timezone.now()
                submission.save()

                logger.info(
                    "[DESIGN] Auto-approved: '%s' by %s (auto_approve=True)",
                    title, designer_employee,
                )
            else:
                submission.status = 'pending'
                submission.reviewer = designer_employee.supervisor
                submission.save()

                logger.info(
                    "[DESIGN] Submitted for review: '%s' by %s -> reviewer: %s",
                    title, designer_employee,
                    designer_employee.supervisor or '(no supervisor)',
                )

        return submission

    # ------------------------------------------------------------------
    # Approve Design — supervisor approves
    # ------------------------------------------------------------------
    @staticmethod
    def approve_design(submission_id, reviewer_employee, review_notes=''):
        """
        Approve a design submission.
        Only the assigned reviewer (supervisor), HR manager, or admin can approve.
        Returns the updated DesignSubmission.
        """
        from hr.models import DesignSubmission

        with transaction.atomic():
            submission = DesignSubmission.objects.select_for_update().get(pk=submission_id)

            if submission.status != 'pending':
                raise ValidationError(
                    f"لا يمكن اعتماد تصميم بحالة: {submission.get_status_display()}"
                )

            # --- Permission check ---
            DesignWorkflowService._check_reviewer_permission(submission, reviewer_employee)

            submission.status = 'approved'
            submission.reviewer = reviewer_employee
            submission.review_notes = review_notes
            submission.reviewed_at = timezone.now()
            submission.save(update_fields=[
                'status', 'reviewer', 'review_notes', 'reviewed_at', 'updated_at',
            ])

        logger.info(
            "[DESIGN] Approved: '%s' (#%s) by %s",
            submission.title, submission.pk, reviewer_employee,
        )

        return submission

    # ------------------------------------------------------------------
    # Reject Design — supervisor rejects with notes
    # ------------------------------------------------------------------
    @staticmethod
    def reject_design(submission_id, reviewer_employee, review_notes=''):
        """
        Reject a design submission.
        Returns the updated DesignSubmission.
        """
        from hr.models import DesignSubmission

        with transaction.atomic():
            submission = DesignSubmission.objects.select_for_update().get(pk=submission_id)

            if submission.status != 'pending':
                raise ValidationError(
                    f"لا يمكن رفض تصميم بحالة: {submission.get_status_display()}"
                )

            DesignWorkflowService._check_reviewer_permission(submission, reviewer_employee)

            if not review_notes.strip():
                raise ValidationError("يجب كتابة سبب الرفض أو ملاحظات للمصمم.")

            submission.status = 'rejected'
            submission.reviewer = reviewer_employee
            submission.review_notes = review_notes
            submission.reviewed_at = timezone.now()
            submission.save(update_fields=[
                'status', 'reviewer', 'review_notes', 'reviewed_at', 'updated_at',
            ])

        logger.info(
            "[DESIGN] Rejected: '%s' (#%s) by %s — notes: %s",
            submission.title, submission.pk, reviewer_employee, review_notes[:100],
        )

        return submission

    # ------------------------------------------------------------------
    # Request Revision — supervisor asks for changes
    # ------------------------------------------------------------------
    @staticmethod
    def request_revision(submission_id, reviewer_employee, review_notes=''):
        """
        Request revision on a design — designer must re-upload.
        Returns the updated DesignSubmission.
        """
        from hr.models import DesignSubmission

        with transaction.atomic():
            submission = DesignSubmission.objects.select_for_update().get(pk=submission_id)

            if submission.status != 'pending':
                raise ValidationError(
                    f"لا يمكن طلب تعديل على تصميم بحالة: {submission.get_status_display()}"
                )

            DesignWorkflowService._check_reviewer_permission(submission, reviewer_employee)

            if not review_notes.strip():
                raise ValidationError("يجب كتابة التعديلات المطلوبة.")

            submission.status = 'revision_requested'
            submission.reviewer = reviewer_employee
            submission.review_notes = review_notes
            submission.reviewed_at = timezone.now()
            submission.save(update_fields=[
                'status', 'reviewer', 'review_notes', 'reviewed_at', 'updated_at',
            ])

        logger.info(
            "[DESIGN] Revision requested: '%s' (#%s) by %s",
            submission.title, submission.pk, reviewer_employee,
        )

        return submission

    # ------------------------------------------------------------------
    # Resubmit Design — designer updates after revision request
    # ------------------------------------------------------------------
    @staticmethod
    def resubmit_design(submission_id, designer_employee, design_file=None,
                        preview_image=None, description=None):
        """
        Resubmit a design after revision was requested.
        Resets status to 'pending' for re-review.
        Returns the updated DesignSubmission.
        """
        from hr.models import DesignSubmission

        with transaction.atomic():
            submission = DesignSubmission.objects.select_for_update().get(pk=submission_id)

            if submission.status != 'revision_requested':
                raise ValidationError("لا يمكن إعادة الرفع إلا بعد طلب تعديل من المراجع.")

            if submission.designer_id != designer_employee.pk:
                raise ValidationError("فقط المصمم الأصلي يمكنه إعادة رفع التصميم.")

            if design_file:
                submission.design_file = design_file
            if preview_image:
                submission.preview_image = preview_image
            if description is not None:
                submission.description = description

            # --- Auto-approve check (in case setting changed) ---
            if designer_employee.auto_approve_designs:
                submission.status = 'approved'
                submission.auto_approved = True
                submission.reviewed_at = timezone.now()
                submission.review_notes = ''
            else:
                submission.status = 'pending'
                submission.reviewed_at = None
                submission.review_notes = ''

            submission.save()

        logger.info(
            "[DESIGN] Resubmitted: '%s' (#%s) by %s (auto=%s)",
            submission.title, submission.pk, designer_employee,
            designer_employee.auto_approve_designs,
        )

        return submission

    # ------------------------------------------------------------------
    # Internal: Check reviewer permission
    # ------------------------------------------------------------------
    @staticmethod
    def _check_reviewer_permission(submission, reviewer_employee):
        """
        Verify that the reviewer has permission to act on this submission.
        Allowed: assigned supervisor, HR manager, or superuser.
        """
        is_assigned_reviewer = (
            submission.reviewer_id is not None
            and submission.reviewer_id == reviewer_employee.pk
        )
        is_supervisor = (
            submission.designer.supervisor_id is not None
            and submission.designer.supervisor_id == reviewer_employee.pk
        )
        is_hr_or_admin = (
            reviewer_employee.is_hr_manager
            or reviewer_employee.user.is_superuser
        )

        if not (is_assigned_reviewer or is_supervisor or is_hr_or_admin):
            raise ValidationError(
                "ليس لديك صلاحية مراجعة هذا التصميم. "
                "فقط المدير المباشر أو مدير الموارد البشرية يمكنه المراجعة."
            )
