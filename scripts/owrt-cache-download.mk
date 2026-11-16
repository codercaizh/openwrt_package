  # Check names carried over from a different download identity before Make
  # decides that an existing archive already satisfies the download target.
  $(if $(and $(OWRT_RECHECK_CACHED_DOWNLOADS),$(filter download,$(MAKECMDGOALS)),$(wildcard $(DL_DIR)/$(FILE))), \
    $(if $(shell sh "$(TOPDIR)/scripts/owrt-cache-download.sh" "$(DL_DIR)/$(FILE)" "$(firstword $(filter-out x skip,$(HASH) $(MIRROR_HASH) $(MD5SUM) $(MIRROR_MD5SUM)))" "$(TMP_DIR)"), \
      $(info CACHE_DOWNLOAD rechecking rejected file=$(FILE))))
