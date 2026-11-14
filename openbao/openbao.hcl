# OpenBao on zara — localhost only (use an SSH tunnel for the web UI)
ui           = true
api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"

storage "raft" {
  path    = "/var/lib/openbao/data"
  node_id = "zara"
}

listener "tcp" {
  address     = "127.0.0.1:8200"
  tls_disable = true
}
