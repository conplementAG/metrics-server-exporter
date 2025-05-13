# -*- coding: utf-8 -*-

import datetime
import getopt
import logging
import os
import requests
import string
import sys
import time

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from prometheus_client import Metric, start_http_server
from prometheus_client.core import REGISTRY

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class MetricsServerExporter:

    def __init__(self):
        logging.info("Initializing MetricsServerExporter")
        self.svc_token       = os.environ.get('K8S_FILEPATH_TOKEN', '/var/run/secrets/kubernetes.io/serviceaccount/token')
        self.ca_cert         = os.environ.get('K8S_CA_CERT_PATH', '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
        self.api_url         = os.environ.get('K8S_ENDPOINT', 'https://kubernetes.default.svc')
        self.names_blacklist = os.environ.get('NAMES_BLACKLIST', '').split(',')
        self.namespaces = os.environ['NAMESPACE_WHITELIST'].split(',') if 'NAMESPACE_WHITELIST' in os.environ else []
        self.labelSelector   = os.environ.get('LABEL_SELECTOR','')

        self.api_nodes_url   = f"{self.api_url}/apis/metrics.k8s.io/v1beta1/nodes"
        self.api_pods_url    = f"{self.api_url}/apis/metrics.k8s.io/v1beta1/pods"

        self.insecure_tls = self.set_tls_mode()

    def set_tls_mode(self):
        tls = ('--insecure-tls','') in options
        logging.info(f"TLS verification {'disabled' if tls else 'enabled'}")
        return tls

    def get_token(self):
        if os.environ.get('K8S_TOKEN'):
            logging.info("Using token from environment variable")
            return os.environ['K8S_TOKEN']
        if os.path.exists(self.svc_token):
            with open(self.svc_token, 'r') as f:
                token = f.readline()
            logging.info("Using token from service account file")
            return token
        logging.warning("No Kubernetes token found")
        return None  
    
    def set_namespaced_pod_url(self, namespace):
        return f"{self.api_url}/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods?labelSelector={self.labelSelector}"

    def kube_metrics(self):
        logging.info("Querying metrics-server API for metrics")
        headers = { "Authorization": f"Bearer {self.get_token()}" }
        query = { 'labelSelector': self.labelSelector }
        session = requests.Session()
        retry = Retry(total=3, connect=3, backoff_factor=0.1)
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        session.verify = not self.insecure_tls and os.path.exists(self.ca_cert) and self.ca_cert or False
        if session.verify:
            logging.info(f"Using CA cert: {self.ca_cert}")
        elif not self.insecure_tls:
            logging.warning("CA cert not found and TLS not disabled explicitly.")

        pod_data = None
        nodes = {'items': []}   
        if self.namespaces:
            for ns in self.namespaces:
                if not ns:

                    continue
                logging.info(f"Fetching pod metrics from namespace: {ns}")
                url = self.set_namespaced_pod_url(ns)
                
                resp = session.get(url, headers=headers, params=query)
                if not resp.ok:
                    logging.error(f"Failed to fetch pod metrics for namespace {ns}: {resp.status_code} {resp.reason}")
                    continue
                try:
                    data = resp.json()
                except Exception as e:
                    logging.error(f"Error decoding JSON response from namespace {ns}: {e}")
                    continue
                
                pod_data = data if pod_data is None else {'items': pod_data['items'] + data.get('items', [])}
        else:
            logging.info("Fetching pod metrics from all namespaces")
            resp_nodes = session.get(self.api_nodes_url, headers=headers, params=query)
            resp_pods = session.get(self.api_pods_url, headers=headers, params=query)
            if not resp_nodes.ok:
                logging.error(f"Failed to fetch node metrics: {resp_nodes.status_code} {resp_nodes.reason}")
                nodes = {'items': []}
            else:
                try:
                    nodes = resp_nodes.json()
                    logging.debug(f"Node: {nodes}")
                except Exception as e:
                    logging.error(f"Error decoding node metrics JSON: {e}")
                    nodes = {'items': []}    

            if not resp_pods.ok:
                logging.error(f"Failed to fetch pod metrics: {resp_pods.status_code} {resp_pods.reason}")
                pod_data = {'items': []}
            else:
                try:
                    pod_data = resp_pods.json()
                    logging.debug(f"Pods: {pod_data}")
                except Exception as e:
                    logging.error(f"Error decoding pod metrics JSON: {e}")
                    pod_data = {'items': []}      

        logging.info("Fetched node and pod metrics successfully")

        return {'nodes': nodes, 'pods': pod_data}

    def collect(self):
        start_time = datetime.datetime.now()
        metrics = self.kube_metrics()
        end_time = datetime.datetime.now()
        total_time = (end_time - start_time).total_seconds()
        logging.info(f"Metrics collection took {total_time:.2f} seconds")

        metrics_response_time = Metric('kube_metrics_server_response_time', 'Metrics Server API Response Time', 'gauge')
        metrics_response_time.add_sample('kube_metrics_server_response_time', value=total_time, labels={ 'api_url': f"{self.api_url}/metrics.k8s.io" })
        yield metrics_response_time

        metrics_nodes_mem = Metric('kube_metrics_server_nodes_mem', 'Metrics Server Nodes Memory', 'gauge')
        metrics_nodes_cpu = Metric('kube_metrics_server_nodes_cpu', 'Metrics Server Nodes CPU', 'gauge')

        for node in metrics['nodes'].get('items', []):
            name = node['metadata']['name']
            cpu = int(node['usage']['cpu'].translate(str.maketrans('', '', string.ascii_letters)))
            mem = int(node['usage']['memory'].translate(str.maketrans('', '', string.ascii_letters)))

            metrics_nodes_cpu.add_sample('kube_metrics_server_nodes_cpu', value=cpu, labels={ 'instance': name })
            metrics_nodes_mem.add_sample('kube_metrics_server_nodes_mem', value=mem, labels={ 'instance': name })
            logging.debug(f"Node {name}: CPU={cpu}, MEM={mem}")

        yield metrics_nodes_mem
        yield metrics_nodes_cpu

        metrics_pods_mem = Metric('kube_metrics_server_pods_mem', 'Metrics Server Pods Memory', 'gauge')
        metrics_pods_cpu = Metric('kube_metrics_server_pods_cpu', 'Metrics Server Pods CPU', 'gauge')

        for pod in metrics['pods'].get('items', []):
            pod_name = pod['metadata']['name']
            pod_namespace = pod['metadata']['namespace']

            for container in pod['containers']:
                cname = container['name']
                cpu = int(container['usage']['cpu'].translate(str.maketrans('', '', string.ascii_letters)))
                mem = int(container['usage']['memory'].translate(str.maketrans('', '', string.ascii_letters)))

                if not any(name in self.names_blacklist for name in [cname, pod_name, pod_namespace]):
                    metrics_pods_cpu.add_sample('kube_metrics_server_pods_cpu', value=cpu, labels={ 'pod_name': pod_name, 'pod_namespace': pod_namespace, 'pod_container_name': cname })
                    metrics_pods_mem.add_sample('kube_metrics_server_pods_mem', value=mem, labels={ 'pod_name': pod_name, 'pod_namespace': pod_namespace, 'pod_container_name': cname })
                    logging.debug(f"Pod {pod_namespace}/{pod_name} - {cname}: CPU={cpu}, MEM={mem}")

        yield metrics_pods_mem
        yield metrics_pods_cpu

if __name__ == '__main__':
    options, remainder = getopt.gnu_getopt(sys.argv[1:], '', ['insecure-tls'])
    logging.info("Starting metrics-server-exporter on port 8000")
    REGISTRY.register(MetricsServerExporter())
    start_http_server(8000)
    while True:
        time.sleep(5)
