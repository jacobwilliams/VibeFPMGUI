!> Passes normally; fails when the "failing-test" feature (macro DEMO_FAIL)
!> is enabled, so you can see a failed test and its exit code.
program check_feature
    implicit none
#ifdef DEMO_FAIL
    print '(a)', "the failing-test feature is on, so this test fails on purpose"
    error stop 1
#else
    print '(a)', "feature test passed (enable the failing-test feature to make it fail)"
#endif
end program check_feature
