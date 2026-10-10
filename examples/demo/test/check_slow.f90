!> Takes about four seconds, so there's time to watch the Tests tab: the
!> spinner, the progress bar and (on later runs) the time estimate.
program check_slow
    implicit none
    integer :: step
    do step = 1, 4
        call wait_seconds(1.0)
        print '(a,i0,a)', "slow test: step ", step, " of 4"
    end do
    print '(a)', "slow test passed"

contains

    !> Portable busy-wait (sleep() is a compiler extension).
    subroutine wait_seconds(seconds)
        real, intent(in) :: seconds
        integer(8) :: start, now, rate
        call system_clock(start, rate)
        do
            call system_clock(now)
            if (real(now - start) / real(rate) >= seconds) exit
        end do
    end subroutine wait_seconds

end program check_slow
