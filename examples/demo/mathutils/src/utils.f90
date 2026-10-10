!> Same file name as the demo's src/utils.f90 (different module), so the
!> build grid has to work out which package each one belongs to.
module mathutils_utils
    use, intrinsic :: iso_fortran_env, only: real64
    implicit none
    private
    public :: wp, clamp
    integer, parameter :: wp = real64

contains

    pure real(wp) function clamp(x, lo, hi)
        real(wp), intent(in) :: x, lo, hi
        clamp = min(max(x, lo), hi)
    end function clamp

end module mathutils_utils
