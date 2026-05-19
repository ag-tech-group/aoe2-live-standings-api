# Override Domain Restricted Sharing at the project level.
#
# The agtechgroup.solutions org enforces `iam.allowedPolicyMemberDomains`,
# which by default rejects IAM bindings that name principals outside the
# org's customer ID. That's the right guardrail at the org level — it
# prevents accidental public exposure across the whole org — but for this
# specific project we genuinely need `allUsers` in the Cloud Run invoker
# policy (the API is the public read surface for tournament streamers).
#
# `allow_all = TRUE` here is scoped to project `aoe2-live-standings-api`
# only; every other project under the org keeps the org-level guardrail.
resource "google_org_policy_policy" "allow_public_iam" {
  name   = "projects/${var.project_id}/policies/iam.allowedPolicyMemberDomains"
  parent = "projects/${var.project_id}"

  spec {
    rules {
      allow_all = "TRUE"
    }
  }
}
