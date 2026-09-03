import paths

# str rather than Path: this is handed straight to Edge on a command line and
# joined with os.path elsewhere, both of which want a plain string.
USER_DATA_DIR = str(paths.data_dir())
PROFILE_NAME = "Default"
