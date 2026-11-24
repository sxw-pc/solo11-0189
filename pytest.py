"""Minimal offline pytest-compatible shim.

This environment has no network and no pytest installed, so this package
provides just enough of the ``python -m pytest`` entry point to run the
unittest-based test suite with the same flags used in the project's
commands.  It is intentionally tiny: discovery, -q and -p no:cacheprovider.
"""
import sys
import unittest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    verbosity = 1
    start_dir = 'tests'
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ('-q', '--quiet'):
            verbosity = 0
        elif arg in ('-v', '--verbose'):
            verbosity = 2
        elif arg == '-p':
            i += 1  # plugin name (e.g. no:cacheprovider) is a no-op
        elif arg.startswith('-p'):
            pass
        elif arg in ('-h', '--help'):
            print('shim pytest: runs unittest discovery; supported: -q -v -p')
            return 0
        elif not arg.startswith('-'):
            start_dir = arg
        i += 1
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
