// (C) Copyright Confidential Containers Contributors
// SPDX-License-Identifier: Apache-2.0

package provisioner

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	log "github.com/sirupsen/logrus"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	nodev1 "k8s.io/api/node/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/e2e-framework/klient/k8s"
	"sigs.k8s.io/e2e-framework/klient/wait"
	"sigs.k8s.io/e2e-framework/klient/wait/conditions"
	"sigs.k8s.io/e2e-framework/pkg/envconf"
)

type PatchLabel struct {
	Op    string `json:"op"`
	Path  string `json:"path"`
	Value string `json:"value"`
}

var Action string

// Adds the worker label to all workers nodes in a given cluster
func AddNodeRoleWorkerLabel(ctx context.Context, clusterName string, cfg *envconf.Config) error {
	fmt.Printf("Adding worker label to nodes belonging to: %s\n", clusterName)
	client, err := cfg.NewClient()
	if err != nil {
		return err
	}

	nodelist := &corev1.NodeList{}
	if err := client.Resources().List(ctx, nodelist); err != nil {
		return err
	}
	// Use full path to avoid overwriting other labels (see RFC 6902)
	payload := []PatchLabel{{
		Op: "add",
		// "/" must be written as ~1 (see RFC 6901)
		Path:  "/metadata/labels/node.kubernetes.io~1worker",
		Value: "",
	}}
	payloadBytes, _ := json.Marshal(payload)
	workerStr := clusterName + "-worker"
	for _, node := range nodelist.Items {
		if strings.Contains(node.Name, workerStr) {
			if err := client.Resources().Patch(ctx, &node, k8s.Patch{PatchType: types.JSONPatchType, Data: payloadBytes}); err != nil {
				return err
			}
		}

	}
	return nil
}

// WaitForDaemonSet waits for a daemonset to be available and all its pods to be ready
// Uses e2e-framework's ResourceMatch condition to check daemonset status, then waits for individual pods
func WaitForDaemonSet(ctx context.Context, cfg *envconf.Config, namespace string, ds *appsv1.DaemonSet, timeout time.Duration) error {
	client, err := cfg.NewClient()
	if err != nil {
		return err
	}
	resources := client.Resources(namespace)

	// Wait for the daemonset to have all desired pods available and ready
	// This uses e2e-framework's ResourceMatch condition with custom logic
	fmt.Printf("Wait for the %s DaemonSet be available\n", ds.GetName())
	if err = wait.For(conditions.New(resources).ResourceMatch(ds, func(object k8s.Object) bool {
		dsObj, ok := object.(*appsv1.DaemonSet)
		if !ok {
			return false
		}
		// Check if all desired pods are scheduled and available
		return dsObj.Status.DesiredNumberScheduled > 0 &&
			dsObj.Status.NumberAvailable == dsObj.Status.DesiredNumberScheduled &&
			dsObj.Status.NumberReady == dsObj.Status.DesiredNumberScheduled
	}), wait.WithTimeout(timeout)); err != nil {
		return err
	}

	// Additionally wait for each pod to be ready to ensure they're fully operational
	pods, err := GetDaemonSetOwnedPods(ctx, cfg, ds)
	if err != nil {
		return err
	}
	for _, pod := range pods.Items {
		fmt.Printf("Wait for the pod %s be ready\n", pod.GetName())
		if err = wait.For(conditions.New(resources).PodReady(&pod), wait.WithTimeout(timeout)); err != nil {
			return err
		}
	}
	return nil
}

// WaitForRuntimeClass waits for a runtimeclass to be created
func WaitForRuntimeClass(ctx context.Context, cfg *envconf.Config, namespace string, runtimeClass *nodev1.RuntimeClass, timeout time.Duration) error {
	client, err := cfg.NewClient()
	if err != nil {
		return err
	}
	resources := client.Resources(namespace)

	log.Infof("Wait for the %s runtimeclass be created\n", runtimeClass.GetName())
	if err = wait.For(conditions.New(resources).ResourcesFound(&nodev1.RuntimeClassList{Items: []nodev1.RuntimeClass{*runtimeClass}}),
		wait.WithTimeout(timeout)); err != nil {
		return err
	}
	return nil
}
