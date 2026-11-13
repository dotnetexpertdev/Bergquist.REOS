import type { DocumentStatus } from '../types';

const STATUS_LABELS: Record<DocumentStatus, string> = {
  received: 'Received',
  reading: 'Reading...',
  extraction_failed: 'Extraction Failed',
  needs_review: 'Needs Review',
  ready_for_approval: 'Ready for Approval',
  duplicate_candidate: 'Possible Duplicate',
  approved: 'Approved',
  bill_created: 'Bill Created',
  attachment_pending: 'Attachment Pending',
  recorded: 'Recorded (Unpaid Bill)',
  linked_existing: 'Linked Existing Bill',
  remote_outcome_unknown: 'QuickBooks Outcome Unknown',
  filed: 'Filed (No Accounting)',
  possible_copy: 'Possible Copy',
  linked_copy: 'Copy of Stored Bill',
};

interface StatusBadgeProps {
  status: DocumentStatus;
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span className={`status-badge status-${status}`}>
      {STATUS_LABELS[status] || status}
    </span>
  );
}
