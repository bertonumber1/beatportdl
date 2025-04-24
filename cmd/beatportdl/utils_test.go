package main

import (
	"os"
	"path"
	"testing"
)

func TestFindConfigFile(t *testing.T) {
	xdgConfigHome := "/tmp/foo/bar"

	t.Run("Use default XDG_CONFIG_HOME without env being set", func(t *testing.T) {
		configFilePath, _, gotErr := FindConfigFile()
		if gotErr != nil {
			t.Errorf("FindConfigFile() failed: %v", gotErr)
			return
		}

		expectedPath := path.Join(os.Getenv("HOME"), ".config", "beatportdl", configFilename)

		if expectedPath != configFilePath {
			t.Errorf("Paths do not match %s != %s", expectedPath, configFilePath)
		}
	})

	t.Run("Use XDG_CONFIG_HOME with env being set", func(t *testing.T) {
		os.Setenv("XDG_CONFIG_HOME", xdgConfigHome)

		configFilePath, _, gotErr := FindConfigFile()
		if gotErr != nil {
			t.Errorf("FindConfigFile() failed: %v", gotErr)
			return
		}

		expectedPath := path.Join(xdgConfigHome, "beatportdl", configFilename)

		if expectedPath != configFilePath {
			t.Errorf("Paths do not match %s != %s", expectedPath, configFilePath)
		}
	})
}

func TestFindCacheFile(t *testing.T) {
	xdgStateHome := "/tmp/foo/bar"

	t.Run("Use default XDG_STATE_HOME without env being set", func(t *testing.T) {
		cacheFilePath, _, gotErr := FindCacheFile()
		if gotErr != nil {
			t.Errorf("FindCacheFile() failed: %v", gotErr)
			return
		}

		expectedPath := path.Join(os.Getenv("HOME"), ".local/state", "beatportdl", cacheFilename)

		if expectedPath != cacheFilePath {
			t.Errorf("Paths do not match %s != %s", expectedPath, cacheFilePath)
		}
	})

	t.Run("Use XDG_STATE_HOME with env being set", func(t *testing.T) {
		os.Setenv("XDG_STATE_HOME", xdgStateHome)

		cacheFilePath, _, gotErr := FindCacheFile()
		if gotErr != nil {
			t.Errorf("FindCacheFile() failed: %v", gotErr)
			return
		}

		expectedPath := path.Join(xdgStateHome, "beatportdl", cacheFilename)

		if expectedPath != cacheFilePath {
			t.Errorf("Paths do not match, %s != %s", expectedPath, cacheFilePath)
		}
	})
}
