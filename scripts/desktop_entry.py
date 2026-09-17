"""PyInstaller entrypoint; child modes share the frozen dependency bundle."""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from wenyi_api.desktop.launcher import main

    raise SystemExit(main())
