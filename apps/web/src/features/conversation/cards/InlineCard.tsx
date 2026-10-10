/**
 * The inline card above the composer (IA §6; mockups `.icard`, component sheet "Inline cards:
 * above the composer, never pinned to an edge"): icon, title, optional dismiss, body, actions.
 * Tones: permission (primary container), stall (warning container), error (error container).
 */
import type { ReactNode } from 'react';
import { IconButton } from '@/ui/controls';
import { Icon, cx, type IconName } from '@/ui/primitives';
import styles from './Cards.module.css';

export type InlineCardTone = 'permission' | 'stall' | 'error';

export interface InlineCardProps {
  readonly tone: InlineCardTone;
  readonly icon: IconName;
  readonly title: ReactNode;
  readonly children?: ReactNode;
  /** Buttons, right-aligned (the confirming action last). */
  readonly actions?: ReactNode;
  /** Left of the actions (e.g. "Or type below to give feedback"). */
  readonly hint?: ReactNode;
  readonly onDismiss?: () => void;
  readonly dismissLabel?: string;
  readonly role?: 'group' | 'status' | 'alert' | 'region';
  readonly 'aria-label'?: string;
  readonly className?: string;
}

export function InlineCard({
  tone,
  icon,
  title,
  children,
  actions,
  hint,
  onDismiss,
  dismissLabel = 'Dismiss',
  role = 'group',
  className,
  ...aria
}: InlineCardProps) {
  return (
    <div className={cx(styles.card, styles[tone], className)} role={role} aria-label={aria['aria-label']} data-card={tone}>
      <div className={styles.head}>
        <Icon name={icon} size={24} className={styles.icon} />
        <div className={styles.title}>{title}</div>
        {onDismiss ? <IconButton icon="close" size="small" iconSize={20} aria-label={dismissLabel} className={styles.dismiss} onClick={onDismiss} /> : null}
      </div>
      {children ? <div className={styles.body}>{children}</div> : null}
      {actions || hint ? (
        <div className={styles.actions}>
          {hint ? <span className={styles.hint}>{hint}</span> : null}
          <span className={styles.buttons}>{actions}</span>
        </div>
      ) : null}
    </div>
  );
}
