package testutils

import (
	"os"
	"testing"
)

//skipTestIfNotRoot skips the test if not running as root user.
func SkipTestIfNotRoot(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("This test requires root privileges. Skipping.")
	}
}
