"""py2app build config — `python setup.py py2app` produces dist/Voitta Desktop.app.

Build deps (separate from runtime requirements.txt):
    pip install py2app

Driver: scripts/build_app.sh (clean, icns, build, optional sign, optional notarize).
"""
from setuptools import setup

APP = ['app.py']

# Bundled into Contents/Resources/. Package data (mcpproxy/backends.yaml,
# ui/settings.html) ships automatically because the dirs are listed in `packages`.
DATA_FILES = [
    ('images', [
        'images/icon.png',
        'images/icon_menubar.png',
        'images/icon_menubar_bright.png',
    ]),
    ('mcpproxy', ['mcpproxy/backends.yaml']),
    ('ui', ['ui/settings.html']),
    ('', ['.env.sample']),
]

OPTIONS = {
    'arch': 'arm64',
    'iconfile': 'images/AppIcon.icns',

    'plist': {
        'CFBundleName': 'Voitta Desktop',
        'CFBundleDisplayName': 'Voitta Desktop',
        'CFBundleIdentifier': 'ai.voitta.desktop',
        'CFBundleVersion': '0.1.0',
        'CFBundleShortVersionString': '0.1.0',
        'LSUIElement': True,
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSHumanReadableCopyright': 'Copyright (c) 2026 Voitta. AGPLv3.',
    },

    'packages': [
        'rumps',
        'aiohttp',
        'fastmcp',
        'msal',
        'requests',
        'PIL',
        'yaml',
        'dotenv',
        'ui',
        'proxy',
        'mcpproxy',
        'middleware',
        'auth',
        'optimizers',
    ],
    'includes': [
        'objc',
        'Cocoa',
        'WebKit',
        'Foundation',
        'AppKit',
    ],
    'excludes': [
        'tkinter',
        'unittest',
        'test',
        'tests',
        'pydoc_data',
        'lib2to3',
        'numpy',
        'pandas',
        'matplotlib',
        'scipy',
    ],

    'argv_emulation': False,
    'site_packages': True,
    'strip': True,
    'optimize': 2,
}

setup(
    app=APP,
    name='Voitta Desktop',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
