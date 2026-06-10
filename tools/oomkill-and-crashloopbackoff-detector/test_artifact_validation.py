#!/usr/bin/env python3
"""
Test cases for is_artifact_meaningful() improvements
"""


# Mock the function for testing (copy-paste the improved version)
def is_artifact_meaningful(content: str) -> bool:
    """
    Check if artifact content is meaningful (not just 'pod not found' errors).

    Uses a two-stage approach:
    1. Size check: If content is reasonably large (>2KB), assume it's meaningful
    2. Pattern check: For small content, look for specific 'oc' error patterns

    Returns:
        True if content has useful information, False if pod was deleted/not found
    """
    if not content or not content.strip():
        return False

    # Stage 1: Size-based heuristic
    MEANINGFUL_SIZE_THRESHOLD = 2048  # 2KB
    content_size = len(content.encode("utf-8"))

    if content_size >= MEANINGFUL_SIZE_THRESHOLD:
        return True

    # Stage 2: Pattern matching for small content (< 2KB)
    lower = content.lower()
    lines = content.strip().split("\n")
    num_lines = len(lines)

    # Small files (< 10 lines) with specific 'oc' error patterns are likely not meaningful
    if num_lines < 10:
        # Check for exact "Error from server (NotFound): pods "..." not found" pattern
        if "error from server" in lower and "notfound" in lower and "pods" in lower:
            return False
        # Check for "Error from server: pods "..." not found" pattern
        if "error from server" in lower and "not found" in lower and "pods" in lower:
            return False
        # Check for standalone "pod not found" errors
        for line in lines:
            line_lower = line.lower().strip()
            if line_lower.startswith("error") and "pod" in line_lower and "not found" in line_lower:
                return False

    return True


# Test cases
def test_empty_content():
    """Empty or whitespace-only content should return False"""
    assert not is_artifact_meaningful("")
    assert not is_artifact_meaningful("   ")
    assert not is_artifact_meaningful("\n\n")
    print("✅ Empty content test passed")


def test_typical_oc_errors():
    """Typical 'oc' pod not found errors should return False"""

    # Standard NotFound error
    error1 = 'Error from server (NotFound): pods "my-pod-abc123" not found'
    assert not is_artifact_meaningful(error1)

    # Alternative format
    error2 = 'Error from server: pods "my-pod-xyz789" not found'
    assert not is_artifact_meaningful(error2)

    # With extra whitespace
    error3 = '\nError from server (NotFound): pods "test-pod" not found\n'
    assert not is_artifact_meaningful(error3)

    print("✅ Typical 'oc' errors test passed")


def test_small_meaningful_content():
    """Small but meaningful content should return True"""

    # Small log with actual error (not pod-not-found)
    log1 = """
    [ERROR] Database connection failed
    [ERROR] Retrying...
    [CRITICAL] OOMKilled - memory limit exceeded
    """
    assert is_artifact_meaningful(log1)

    # Config or status output (8 lines)
    log2 = """
    Name: my-pod
    Status: Running
    Memory: 512Mi
    CPU: 100m
    Restarts: 2
    Age: 5h
    Events: <none>
    """
    assert is_artifact_meaningful(log2)

    print("✅ Small meaningful content test passed")


def test_large_content_always_meaningful():
    """Content > 2KB should always return True, even with 'not found' in it"""

    # Large log (>2KB) with "not found" somewhere buried in it
    large_log = "2026-06-03 10:00:00 Starting application...\n"
    large_log += "2026-06-03 10:00:01 Loading config...\n" * 50
    large_log += "2026-06-03 10:00:10 [WARNING] Config file not found, using defaults\n"
    large_log += "2026-06-03 10:00:11 Processing...\n" * 50
    large_log += "2026-06-03 10:00:20 [ERROR] OOM killed\n"

    assert len(large_log.encode("utf-8")) > 2048, "Test log should be > 2KB"
    assert is_artifact_meaningful(large_log)

    print("✅ Large content test passed")


def test_edge_case_not_found_in_meaningful_log():
    """10+ line logs with 'not found' in middle should return True"""

    # 15 lines with "not found" in the middle, but NOT an oc error
    log = """
    [INFO] Application starting
    [INFO] Loading modules
    [INFO] Connecting to database
    [ERROR] Connection to cache.example.com not found
    [INFO] Retrying connection
    [INFO] Connected successfully
    [WARNING] High memory usage detected
    [CRITICAL] Memory limit approaching
    [ERROR] OOMKilled - container exceeded memory limit
    [INFO] Container restarting
    [INFO] Health check failed
    [ERROR] Liveness probe failed
    [INFO] Pod terminating
    [INFO] Exit code: 137
    """

    lines = log.strip().split("\n")
    assert len(lines) >= 10, "Should have 10+ lines"
    assert is_artifact_meaningful(log)

    print("✅ Edge case 'not found' in meaningful log test passed")


def test_multi_line_oc_error():
    """Multi-line oc error output (< 10 lines) should return False"""

    error = """
Error from server (NotFound): pods "pipeline-run-xyz" not found

Command: oc logs pipeline-run-xyz -n build-namespace
"""

    assert not is_artifact_meaningful(error)
    print("✅ Multi-line oc error test passed")


def test_exact_threshold_2kb():
    """Test content exactly at 2KB boundary"""

    # Just under 2KB
    content_under = "x" * 2047
    # Exactly 2KB
    content_exact = "x" * 2048
    # Just over 2KB
    content_over = "x" * 2049

    # All should be meaningful (no error patterns)
    assert is_artifact_meaningful(content_under)
    assert is_artifact_meaningful(content_exact)
    assert is_artifact_meaningful(content_over)

    print("✅ 2KB threshold test passed")


def run_all_tests():
    """Run all test cases"""
    print("\n" + "=" * 60)
    print("Testing improved is_artifact_meaningful() function")
    print("=" * 60 + "\n")

    test_empty_content()
    test_typical_oc_errors()
    test_small_meaningful_content()
    test_large_content_always_meaningful()
    test_edge_case_not_found_in_meaningful_log()
    test_multi_line_oc_error()
    test_exact_threshold_2kb()

    print("\n" + "=" * 60)
    print("✅ All tests passed!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_all_tests()
