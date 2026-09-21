"""Generate build metadata; no author identity or local project path is embedded."""
import plistlib
import sys
from pathlib import Path
metadata=dict(CFBundleName='Session Writer',CFBundleDisplayName='Session Writer',
    CFBundleIdentifier='org.sessionwriter.desktop',CFBundleExecutable='session-writer-ui',
    CFBundlePackageType='APPL',CFBundleShortVersionString='0.1.0',CFBundleVersion='1',
    LSMinimumSystemVersion='13.0',NSHighResolutionCapable=True)
Path(sys.argv[1]).write_bytes(plistlib.dumps(metadata))
