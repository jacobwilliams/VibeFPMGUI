!> Public interface of mathutils.
module mathutils
    use mathutils_utils, only: wp, clamp
    use fmin_module, only: fmin
    implicit none
    private
    public :: clamp, golden_minimum

contains

    !> Minimum of (x-2)**2 on [0, 5], found with fmin (a git dependency).
    real(wp) function golden_minimum()
        golden_minimum = fmin(parabola, 0.0_wp, 5.0_wp, 1.0e-8_wp)
    end function golden_minimum

    real(wp) function parabola(x)
        real(wp), intent(in) :: x
        parabola = (x - 2.0_wp)**2
    end function parabola

end module mathutils
