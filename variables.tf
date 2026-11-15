variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "rolodex"
}

variable "alert_email" {
  description = "Where the budget alarm emails go. Set it in terraform.tfvars, which never goes to GitHub."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Email me when the account's spend this month passes half of this, or is forecast to pass all of it."
  type        = number
  default     = 10
}

variable "min_tasks" {
  type    = number
  default = 2
}

variable "max_tasks" {
  type    = number
  default = 4
}

variable "requests_per_task" {
  description = "Auto scaling target: requests per container per minute before ECS adds another."
  type        = number
  default     = 120
}

variable "endpoint_azs" {
  description = "How many AZs get the interface endpoints. 1 halves their cost; 2 survives losing a zone."
  type        = number
  default     = 1
}
