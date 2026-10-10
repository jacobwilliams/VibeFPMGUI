!> Basic statistics.
module demo_stats
    use demo_kinds, only: wp
    implicit none
    private
    public :: mean, variance

contains

    pure real(wp) function mean(x)
        real(wp), intent(in) :: x(:)
        mean = sum(x) / real(max(size(x), 1), wp)
    end function mean

    pure real(wp) function variance(x)
        real(wp), intent(in) :: x(:)
        variance = sum((x - mean(x))**2) / real(max(size(x) - 1, 1), wp)
    end function variance

end module demo_stats
