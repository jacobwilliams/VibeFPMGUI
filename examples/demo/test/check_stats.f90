!> Unit tests written with test-drive (the dev-dependency).
program check_stats
    use, intrinsic :: iso_fortran_env, only: error_unit
    use testdrive, only: run_testsuite, new_unittest, unittest_type, error_type, check
    use demo, only: wp, mean, variance, linspace, clamp
    implicit none
    integer :: stat
    stat = 0
    call run_testsuite(collect, error_unit, stat)
    if (stat > 0) then
        write (error_unit, '(i0,a)') stat, " test(s) failed"
        error stop 1
    end if

contains

    subroutine collect(testsuite)
        type(unittest_type), allocatable, intent(out) :: testsuite(:)
        testsuite = [new_unittest("mean", test_mean), &
                     new_unittest("variance", test_variance), &
                     new_unittest("linspace", test_linspace), &
                     new_unittest("clamp", test_clamp)]
    end subroutine collect

    subroutine test_mean(error)
        type(error_type), allocatable, intent(out) :: error
        call check(error, mean([1.0_wp, 2.0_wp, 3.0_wp]), 2.0_wp)
    end subroutine test_mean

    subroutine test_variance(error)
        type(error_type), allocatable, intent(out) :: error
        call check(error, variance([1.0_wp, 2.0_wp, 3.0_wp]), 1.0_wp)
    end subroutine test_variance

    subroutine test_linspace(error)
        type(error_type), allocatable, intent(out) :: error
        real(wp), allocatable :: x(:)
        x = linspace(0.0_wp, 1.0_wp, 5)
        call check(error, size(x), 5)
        if (allocated(error)) return
        call check(error, x(5), 1.0_wp)
    end subroutine test_linspace

    subroutine test_clamp(error)
        type(error_type), allocatable, intent(out) :: error
        call check(error, clamp(7.0_wp, 0.0_wp, 1.0_wp), 1.0_wp)
    end subroutine test_clamp

end program check_stats
