from sqlalchemy import select

from fair.schemas.db import AuditEvent, SystemState


class KillSwitch:
    def __init__(self, sessions):
        self.sessions = sessions

    @property
    def stopped(self):
        with self.sessions() as session:
            state = session.get(SystemState, "global")
            return state is None or state.stopped

    def set(self, stopped: bool, actor_id="admin"):
        with self.sessions.begin() as session:
            state = session.scalar(
                select(SystemState).where(SystemState.id == "global").with_for_update()
            )
            if state is None:
                raise RuntimeError("System state not initialized")
            state.stopped = stopped
            session.add(
                AuditEvent(
                    actor_id=actor_id,
                    event_type="SYSTEM_STOP" if stopped else "SYSTEM_RESUME",
                    payload_json={"stopped": stopped},
                )
            )
