!> Small helpers. (mathutils also has a file called utils.f90, which the
!> build grid tells apart by package.)
module demo_utils
    use demo_kinds, only: wp
    implicit none
    private
    public :: linspace, to_string

contains

    pure function linspace(a, b, n) result(x)
        real(wp), intent(in) :: a, b
        integer, intent(in) :: n
        real(wp) :: x(n)
        integer :: i
        do i = 1, n
            x(i) = a + (b - a) * real(i - 1, wp) / real(max(n - 1, 1), wp)
        end do
    end function linspace

    function to_string(x) result(s)
        real(wp), intent(in) :: x
        character(len=:), allocatable :: s
        character(len=32) :: buffer
        write (buffer, '(g0.6)') x
        s = trim(adjustl(buffer))
    end function to_string

end module demo_utils
