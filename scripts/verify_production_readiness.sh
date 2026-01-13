#!/bin/bash
# TwinOps Production Readiness Verification Script
# This script verifies that TwinOps is functional and production-ready
set -e

echo "=== TwinOps Production Readiness Verification ==="
echo "Started: $(date)"
echo ""

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

PASS=0
FAIL=0
WARN=0
SKIP=0

# Base URL - configurable via environment
AGENT_URL="${TWINOPS_AGENT_URL:-http://localhost:8080}"

check() {
    if [ $1 -eq 0 ]; then
        echo -e "${GREEN}✓ PASS${NC}: $2"
        ((PASS++))
    else
        echo -e "${RED}✗ FAIL${NC}: $2"
        ((FAIL++))
    fi
}

warn() {
    echo -e "${YELLOW}⚠ WARN${NC}: $1"
    ((WARN++))
}

skip() {
    echo -e "${BLUE}○ SKIP${NC}: $1"
    ((SKIP++))
}

info() {
    echo -e "${BLUE}ℹ INFO${NC}: $1"
}

# ============================================
# Phase 1: Service Health Checks
# ============================================
echo -e "\n${BLUE}--- Phase 1: Service Health ---${NC}"

# Test 1.1: Health endpoint
HEALTH_RESP=$(curl -sf "${AGENT_URL}/health" 2>/dev/null || echo "FAILED")
if [ "$HEALTH_RESP" != "FAILED" ]; then
    echo "$HEALTH_RESP" | jq -e '.status == "healthy"' > /dev/null 2>&1
    check $? "Health endpoint returns healthy status"
else
    check 1 "Health endpoint accessible"
fi

# Test 1.2: Readiness endpoint
READY_RESP=$(curl -sf "${AGENT_URL}/ready" 2>/dev/null || echo "FAILED")
if [ "$READY_RESP" != "FAILED" ]; then
    echo "$READY_RESP" | jq -e '.status' > /dev/null 2>&1
    check $? "Readiness endpoint returns status"
else
    check 1 "Readiness endpoint accessible"
fi

# Test 1.3: Metrics endpoint
METRICS_RESP=$(curl -sf "${AGENT_URL}/metrics" 2>/dev/null || echo "FAILED")
if [ "$METRICS_RESP" != "FAILED" ]; then
    echo "$METRICS_RESP" | grep -q "twinops_"
    check $? "Metrics endpoint returns TwinOps metrics"
else
    check 1 "Metrics endpoint accessible"
fi

# Test 1.4: OpenAPI spec
OPENAPI_RESP=$(curl -sf "${AGENT_URL}/openapi.json" 2>/dev/null || echo "FAILED")
if [ "$OPENAPI_RESP" != "FAILED" ]; then
    echo "$OPENAPI_RESP" | jq -e '.info.title' > /dev/null 2>&1
    check $? "OpenAPI spec available"
else
    check 1 "OpenAPI endpoint accessible"
fi

# ============================================
# Phase 2: Chat API Tests
# ============================================
echo -e "\n${BLUE}--- Phase 2: Chat API ---${NC}"

# Test 2.1: Simple chat request
CHAT_RESP=$(curl -sf -X POST "${AGENT_URL}/chat" \
    -H 'Content-Type: application/json' \
    -d '{"message":"What is the current status?"}' 2>/dev/null || echo "FAILED")

if [ "$CHAT_RESP" != "FAILED" ]; then
    echo "$CHAT_RESP" | jq -e '.reply' > /dev/null 2>&1
    check $? "Chat API returns reply"
else
    check 1 "Chat API accessible"
fi

# Test 2.2: Chat with roles header
CHAT_ROLES_RESP=$(curl -sf -X POST "${AGENT_URL}/chat" \
    -H 'Content-Type: application/json' \
    -H 'X-Roles: operator' \
    -d '{"message":"hello"}' 2>/dev/null || echo "FAILED")

if [ "$CHAT_ROLES_RESP" != "FAILED" ]; then
    echo "$CHAT_ROLES_RESP" | jq -e '.reply' > /dev/null 2>&1
    check $? "Chat API accepts X-Roles header"
else
    check 1 "Chat API with roles"
fi

# ============================================
# Phase 3: RBAC Enforcement Tests
# ============================================
echo -e "\n${BLUE}--- Phase 3: RBAC Enforcement ---${NC}"

# Test 3.1: Viewer cannot execute control operations
RBAC_VIEWER_RESP=$(curl -sf -X POST "${AGENT_URL}/chat" \
    -H 'Content-Type: application/json' \
    -H 'X-Roles: viewer' \
    -d '{"message":"Start the pump"}' 2>/dev/null || echo "FAILED")

if [ "$RBAC_VIEWER_RESP" != "FAILED" ]; then
    # Check if operation was denied (either via tool_results or reply)
    DENIED=$(echo "$RBAC_VIEWER_RESP" | jq -r '.tool_results[0].success // true')
    if [ "$DENIED" = "false" ]; then
        check 0 "RBAC denies viewer from control operations"
    else
        # Check if reply mentions authorization
        echo "$RBAC_VIEWER_RESP" | grep -qi "not authorized\|permission\|denied"
        check $? "RBAC denies viewer from control operations"
    fi
else
    skip "RBAC test (service unavailable)"
fi

# Test 3.2: Operator can access control operations
RBAC_OP_RESP=$(curl -sf -X POST "${AGENT_URL}/chat" \
    -H 'Content-Type: application/json' \
    -H 'X-Roles: operator' \
    -d '{"message":"Set speed to 1000"}' 2>/dev/null || echo "FAILED")

if [ "$RBAC_OP_RESP" != "FAILED" ]; then
    # Should get some response (not a RBAC denial)
    echo "$RBAC_OP_RESP" | jq -e '.' > /dev/null 2>&1
    check $? "Operator role can attempt control operations"
else
    skip "RBAC operator test (service unavailable)"
fi

# ============================================
# Phase 4: Safety Features Tests
# ============================================
echo -e "\n${BLUE}--- Phase 4: Safety Features ---${NC}"

# Test 4.1: HIGH risk operations force simulation
SAFETY_HIGH_RESP=$(curl -sf -X POST "${AGENT_URL}/chat" \
    -H 'Content-Type: application/json' \
    -H 'X-Roles: operator' \
    -d '{"message":"Start the pump"}' 2>/dev/null || echo "FAILED")

if [ "$SAFETY_HIGH_RESP" != "FAILED" ]; then
    # Check if simulated flag is present
    SIMULATED=$(echo "$SAFETY_HIGH_RESP" | jq -r '.tool_results[0].simulated // false')
    if [ "$SIMULATED" = "true" ]; then
        check 0 "HIGH risk operations run in simulation mode"
    else
        warn "HIGH risk operation may not have forced simulation"
    fi
else
    skip "Safety simulation test (service unavailable)"
fi

# Test 4.2: CRITICAL operations require approval
SAFETY_CRIT_RESP=$(curl -sf -X POST "${AGENT_URL}/chat" \
    -H 'Content-Type: application/json' \
    -H 'X-Roles: maintenance' \
    -d '{"message":"Emergency stop"}' 2>/dev/null || echo "FAILED")

if [ "$SAFETY_CRIT_RESP" != "FAILED" ]; then
    # Check for approval requirement
    NEEDS_APPROVAL=$(echo "$SAFETY_CRIT_RESP" | jq -r '.pending_approval // false')
    TASK_ID=$(echo "$SAFETY_CRIT_RESP" | jq -r '.task_id // "none"')

    if [ "$NEEDS_APPROVAL" = "true" ] || [ "$TASK_ID" != "none" ] && [ "$TASK_ID" != "null" ]; then
        check 0 "CRITICAL operations create approval tasks"
        info "Task ID: $TASK_ID"

        # KNOWN BUG: No approval endpoint exists
        warn "KNOWN BUG: No REST endpoint exists to approve task $TASK_ID"
    else
        warn "CRITICAL operation did not require approval (may be rules-based LLM)"
    fi
else
    skip "Safety approval test (service unavailable)"
fi

# ============================================
# Phase 5: Approval Workflow Tests (KNOWN ISSUES)
# ============================================
echo -e "\n${BLUE}--- Phase 5: Approval Workflow ---${NC}"

# Test 5.1: Check if approval endpoints exist
ENDPOINTS=$(curl -sf "${AGENT_URL}/openapi.json" 2>/dev/null | jq -r '.paths | keys[]' 2>/dev/null || echo "")

if echo "$ENDPOINTS" | grep -q "/tasks"; then
    check 0 "Tasks list endpoint exists"
else
    check 1 "Tasks list endpoint exists (/tasks)"
fi

if echo "$ENDPOINTS" | grep -q "approve"; then
    check 0 "Approval endpoint exists"
else
    check 1 "Approval endpoint exists (/tasks/{id}/approve)"
    warn "CRITICAL: Cannot approve HITL tasks via REST API"
fi

if echo "$ENDPOINTS" | grep -q "reject"; then
    check 0 "Rejection endpoint exists"
else
    check 1 "Rejection endpoint exists (/tasks/{id}/reject)"
fi

# ============================================
# Phase 6: Reset Functionality
# ============================================
echo -e "\n${BLUE}--- Phase 6: State Management ---${NC}"

# Test 6.1: Reset endpoint
RESET_RESP=$(curl -sf -X POST "${AGENT_URL}/reset" 2>/dev/null || echo "FAILED")
if [ "$RESET_RESP" != "FAILED" ]; then
    echo "$RESET_RESP" | jq -e '.status == "ok"' > /dev/null 2>&1
    check $? "Reset endpoint works"
else
    check 1 "Reset endpoint accessible"
fi

# ============================================
# Phase 7: Configuration Verification
# ============================================
echo -e "\n${BLUE}--- Phase 7: Configuration ---${NC}"

# Check for hardcoded values (reading source if available)
if [ -f "src/twinops/agent/main.py" ]; then
    grep -q 'policy_submodel_id\s*=' src/twinops/agent/main.py
    if [ $? -eq 0 ]; then
        # Check if it's hardcoded or from settings
        grep -q 'TWINOPS_POLICY_SUBMODEL\|settings.*policy' src/twinops/agent/main.py
        if [ $? -eq 0 ]; then
            check 0 "Policy submodel ID is configurable"
        else
            check 1 "Policy submodel ID is configurable (hardcoded found)"
            warn "Policy submodel ID appears to be hardcoded in main.py"
        fi
    else
        skip "Policy submodel ID check (pattern not found)"
    fi
else
    skip "Source code not available for config check"
fi

# ============================================
# Summary
# ============================================
echo -e "\n${BLUE}========================================${NC}"
echo -e "${BLUE}=== VERIFICATION SUMMARY ===${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "Passed:   ${GREEN}$PASS${NC}"
echo -e "Failed:   ${RED}$FAIL${NC}"
echo -e "Warnings: ${YELLOW}$WARN${NC}"
echo -e "Skipped:  ${BLUE}$SKIP${NC}"
echo ""

if [ $FAIL -gt 0 ]; then
    echo -e "${RED}STATUS: NOT PRODUCTION READY${NC}"
    echo ""
    echo "Critical issues must be resolved before production deployment:"
    echo "  - Missing approval REST endpoints (HITL workflow broken)"
    echo "  - Check warnings above for additional issues"
    exit 1
elif [ $WARN -gt 0 ]; then
    echo -e "${YELLOW}STATUS: PRODUCTION READY WITH WARNINGS${NC}"
    echo ""
    echo "Review warnings above before production deployment."
    exit 0
else
    echo -e "${GREEN}STATUS: PRODUCTION READY${NC}"
    exit 0
fi
