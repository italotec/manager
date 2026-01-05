import threading
from flask import current_app
from . import db
from .models import Job
from .services.waba_flow import process_one_waba_add_phone

def start_add_phone_job(user_id: int, waba_ids: list[str]) -> int:
    job = Job(
        user_id=user_id,
        type="add_phone",
        status="queued",
        total=len(waba_ids),
        done=0
    )
    # optional fields if your Job model has them; harmless if not
    if hasattr(job, "failed"):
        job.failed = 0

    db.session.add(job)
    db.session.commit()

    job_id = job.id

    def runner(app):
        with app.app_context():
            job = db.session.get(Job, job_id)
            if not job:
                return

            job.status = "running"
            db.session.commit()

            failed = 0

            for idx, waba_id in enumerate(waba_ids, start=1):
                job.current_label = str(waba_id)
                job.last_message = f"Processando {idx}/{len(waba_ids)}"
                db.session.commit()

                ok = process_one_waba_add_phone(user_id=user_id, waba_id=str(waba_id), job_id=job_id)
                if not ok:
                    failed += 1
                    # keep job running, but record it
                    job.last_message = f"Falhou em {waba_id} (veja logs/last_error no bms.json)"
                    db.session.commit()

                job.done = idx
                if hasattr(job, "failed"):
                    job.failed = failed
                db.session.commit()

            job.status = "done" if failed == 0 else "done_with_errors"
            job.last_message = "Finalizado." if failed == 0 else f"Finalizado com erros ({failed})."
            db.session.commit()

    t = threading.Thread(target=runner, args=(current_app._get_current_object(),), daemon=True)
    t.start()

    return job_id
