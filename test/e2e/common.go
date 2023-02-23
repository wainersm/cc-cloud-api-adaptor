package e2e

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"os"
	"os/exec"
	"testing"
)

// ConvertQcow2ToRaw Converts an qcow2 image to raw. Requires `qemu-img` installed.
func ConvertQcow2ToRaw(qcow2 string, raw string) error {
	cmd := exec.Command("qemu-img", "convert", "-O", "raw", qcow2, raw)
	cmd.Stderr = os.Stderr
	err := cmd.Run()
	if err != nil {
		return err
	}

	return nil
}

// newPod returns a new Pod object.
func newPod(namespace string, name string, containerName string, runtimeclass string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: corev1.PodSpec{
			Containers:       []corev1.Container{{Name: containerName, Image: "nginx"}},
			DNSPolicy:        "ClusterFirst",
			RestartPolicy:    "Never",
			RuntimeClassName: &runtimeclass,
		},
	}
}

// CloudAssert defines assertions to perform on the cloud provider.
type CloudAssert interface {
	HasPodVM(t *testing.T, id string) // Assert there is a PodVM with `id`.
}
