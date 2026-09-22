"""Run separately from the API. Durable retries survive a process restart."""
import time
from .api import create_app


def main():
    store = create_app().state.store
    while True:
        for job_id in store.due():
            try:
                store.run(job_id)
            except Exception as exc:
                print(type(exc).__name__, flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
