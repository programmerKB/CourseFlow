"""Apply the deployment's explicit 115-1 initial registration dates."""
import argparse
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from .config import settings
from .db import SessionLocal
from .models import AllocationRun, RegistrationPeriod, Term
from .registration import audit
from .seed import uid


def configure_initial_window(*, check_only=False, allow_missing=False):
    start, end = settings.initial_start_at, settings.initial_end_at
    if start is None:
        return False
    term_id = uid('term/main')
    with SessionLocal.begin() as db:
        # Checking may happen before the first migration. A fresh installation will seed the term.
        if check_only and db.scalar(text("SELECT to_regclass('registration_periods')")) is None:
            if allow_missing:
                return False
            raise ValueError('資料庫尚未初始化，請先部署 115-1。')
        term = db.scalar(select(Term.id).where(Term.id == term_id).with_for_update())
        period = db.get(RegistrationPeriod, (term_id, 'INITIAL')) if term else None
        if period is None:
            if check_only and allow_missing and term is None:
                return False
            raise ValueError('找不到 115-1 初選時段。請先初始化此學期。')
        changed = (period.starts_at, period.ends_at) != (start, end)
        if changed and db.scalar(select(AllocationRun.id).where(AllocationRun.term_id == term_id)) is not None:
            raise ValueError('115-1 已公布分發結果，無法重設本次初選時段。')
        if settings.public_initial_term_id != term_id:
            add_drop = db.get(RegistrationPeriod, (term_id, 'ADD_DROP'))
            if add_drop is None or end > add_drop.starts_at:
                raise ValueError('請先設定晚於初選截止的加退選時段。')
        if check_only or not changed:
            return False
        previous = {'starts_at': period.starts_at, 'ends_at': period.ends_at}
        period.starts_at, period.ends_at = start, end
        audit(db, None, term_id, 'INITIAL_PERIOD_CONFIGURED', 'term', term_id,
              {'previous': previous, 'starts_at': start, 'ends_at': end})
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--allow-missing', action='store_true')
    args = parser.parse_args()
    try:
        updated = configure_initial_window(check_only=args.check, allow_missing=args.allow_missing)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if updated:
        taipei = ZoneInfo('Asia/Taipei')
        start = settings.initial_start_at.astimezone(taipei)
        end = settings.initial_end_at.astimezone(taipei)
        print(f'115-1 初選時段已更新　{start:%Y/%m/%d %H:%M} 至 {end:%Y/%m/%d %H:%M}（臺北時間）')


if __name__ == '__main__':
    main()
