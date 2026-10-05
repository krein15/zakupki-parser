"""Entry point of the desktop application (also used by PyInstaller).

Started without arguments it opens the window; with arguments it is the command line, so the built program
also runs the scheduled checks:

    ZakupkiParser.exe monitor "профили/канцтовары.toml"
"""

import sys

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from zkparser.__main__ import main as cli_main
        from zkparser.__main__ import setup_console

        setup_console()
        sys.exit(cli_main())

    from zkparser.gui.app import main

    main()
