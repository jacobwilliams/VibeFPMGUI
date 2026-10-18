!> Something to try the debugger on (Debug → Debug a Test…, or this test's
!> Debug button in Build outputs): every kind of variable, loops, and
!> contained procedures that call each other. It passes (exit code 0), and
!> frees what it allocates, so Valgrind finds nothing either.
!>
!> Good places for a breakpoint (click left of the line number):
!>   * "call fill(grid)" in the main program, then Step Into
!>   * "total = total + i * weight(by)" in sum_loop, to watch a loop's variables
!>   * "f = n * factorial(n - 1)" in factorial: the call stack grows with each call
!>   * "call p%move(1.5_wp, -0.5_wp)" for a derived type's components
module shapes_for_debugging
    use, intrinsic :: iso_fortran_env, only: int32, real64
    implicit none
    private
    public :: wp, point, particle, moves_made

    integer, parameter :: wp = real64
    integer :: moves_made = 0  !! a module variable (Globals in the Debug panel)

    type :: point
        real(wp) :: x = 0.0_wp
        real(wp) :: y = 0.0_wp
    end type point

    type :: particle
        character(len=12) :: name = "unnamed"
        type(point) :: position  !! a derived type inside another
        real(wp) :: velocity(2) = [0.0_wp, 0.0_wp]
        integer(int32) :: charge = 0
        logical :: active = .true.
        real(wp), allocatable :: history(:)  !! grows with each move
    contains
        procedure :: move
        procedure :: speed
    end type particle

contains

    subroutine move(self, dx, dy)
        class(particle), intent(inout) :: self
        real(wp), intent(in) :: dx, dy
        self%position%x = self%position%x + dx
        self%position%y = self%position%y + dy
        if (.not. allocated(self%history)) then
            allocate (self%history(0))
        end if
        self%history = [self%history, self%position%x]
        moves_made = moves_made + 1
    end subroutine move

    real(wp) function speed(self)
        class(particle), intent(in) :: self
        speed = sqrt(sum(self%velocity**2))
    end function speed

end module shapes_for_debugging


program check_debugger
    use, intrinsic :: iso_fortran_env, only: int8, int16, int64, real32, error_unit
    use shapes_for_debugging, only: wp, point, particle, moves_made
    implicit none

    ! Scalars of each intrinsic type, and a few kinds.
    integer :: count = 0
    integer(int8) :: tiny = 127_int8
    integer(int16) :: small = -32000_int16
    integer(int64) :: big = 9000000000_int64
    real(real32) :: single = 3.25
    real(wp) :: total = 0.0_wp
    real(wp), parameter :: pi = 3.141592653589793_wp
    complex(wp) :: z = (1.0_wp, -2.0_wp)
    logical :: ok = .true.
    logical :: found = .false.
    character(len=16) :: label = "debugger demo"
    character(len=:), allocatable :: message

    ! Arrays: fixed, two-dimensional, allocatable, a pointer, of characters.
    integer :: squares(5)
    real(wp) :: grid(3, 4)
    real(wp), allocatable :: samples(:)
    integer, pointer :: window(:) => null()
    character(len=6) :: weekdays(3) = ["Mon   ", "Tue   ", "Wed   "]

    ! Derived types.
    type(point) :: origin
    type(particle) :: p
    type(particle), allocatable :: swarm(:)

    integer :: i, j

    ! A loop over an array.
    do i = 1, size(squares)
        squares(i) = i * i
    end do
    count = sum(squares)

    ! Nested loops filling a 2-D array, through a contained subroutine.
    call fill(grid)

    ! A loop with a step, in a function with an optional argument.
    total = sum_loop(10)
    total = total + sum_loop(10, step=2)

    ! Recursion: the call stack grows to factorial(1).
    big = big + factorial(6)

    ! An allocatable array, sized at run time, and an elemental function on it.
    allocate (samples(8))
    samples = [(real(i, wp) / 2.0_wp, i = 1, size(samples))]
    samples = clamp(samples, 0.75_wp, 3.0_wp)

    ! A pointer to a section of another array.
    allocate (window(3))
    window = squares(2:4)

    ! A derived type: its components, a nested type, an allocatable component.
    origin = point(0.0_wp, 0.0_wp)
    p%name = "proton"
    p%charge = 1
    p%velocity = [3.0_wp, 4.0_wp]
    call p%move(1.5_wp, -0.5_wp)
    call p%move(0.25_wp, 0.25_wp)

    ! An array of derived types.
    allocate (swarm(3))
    do i = 1, size(swarm)
        write (swarm(i)%name, '(a,i0)') "particle_", i
        swarm(i)%charge = merge(1, -1, mod(i, 2) == 1)
        call swarm(i)%move(real(i, wp), 0.0_wp)
    end do

    ! A do while, exit and cycle, and select case.
    j = 0
    do while (.not. found)
        j = j + 1
        if (mod(j, 2) == 0) cycle
        select case (j)
        case (1:3)
            single = single * 2.0
        case (5)
            found = .true.
        case default
            exit
        end select
    end do

    ! Strings: a deferred-length one, built up.
    message = trim(label) // ": " // describe(count)
    message = message // ", z = " // complex_text(z)

    ! associate and block: names that exist only inside them.
    associate (first => weekdays(1), last => weekdays(size(weekdays)))
        ok = ok .and. first == "Mon" .and. last == "Wed"
    end associate
    block
        real(wp) :: area
        area = pi * 2.0_wp**2
        ok = ok .and. abs(area - 12.566370614359172_wp) < 1.0e-12_wp
    end block

    ! The checks: what the code above should have computed.
    ok = ok .and. count == 55
    ok = ok .and. abs(grid(3, 4) - 28.0_wp) < 1.0e-12_wp
    ok = ok .and. abs(total - 255.0_wp) < 1.0e-12_wp  ! (55, then 2 * (1 + 3 + … + 19))
    ok = ok .and. big == 9000000720_int64
    ok = ok .and. all(window == [4, 9, 16])
    ok = ok .and. abs(samples(1) - 0.75_wp) < 1.0e-12_wp .and. abs(samples(8) - 3.0_wp) < 1.0e-12_wp
    ok = ok .and. abs(p%position%x - 1.75_wp) < 1.0e-12_wp .and. size(p%history) == 2
    ok = ok .and. abs(p%speed() - 5.0_wp) < 1.0e-12_wp
    ok = ok .and. moves_made == 5 .and. swarm(2)%charge == -1
    ok = ok .and. found .and. j == 5 .and. abs(single - 13.0) < 1.0e-6
    ok = ok .and. tiny == 127_int8 .and. small == -32000_int16
    ok = ok .and. message == "debugger demo: 55 (odd), z = (1.0,-2.0)"

    deallocate (samples, swarm, message)
    deallocate (window)
    if (allocated(p%history)) deallocate (p%history)

    if (.not. ok) then
        write (error_unit, '(a)') "check_debugger: a value wasn't what was expected"
        error stop 1
    end if
    print '(a)', "debugger demo passed"

contains

    !> grid(i, j) = i * j + j * j: two nested loops (Step Into this one).
    subroutine fill(a)
        real(wp), intent(out) :: a(:, :)
        integer :: row, col
        do col = 1, size(a, 2)
            do row = 1, size(a, 1)
                a(row, col) = real(row * col + col * col, wp)
            end do
        end do
    end subroutine fill

    !> 1 + 2 + … + n, or (with the optional step) every step-th number up to
    !> 2n, each weighted by the step.
    real(wp) function sum_loop(n, step) result(total)
        integer, intent(in) :: n
        integer, intent(in), optional :: step
        integer :: i, by
        by = 1
        if (present(step)) by = step
        total = 0.0_wp
        do i = 1, 2 * n, by
            if (i > n .and. by == 1) exit
            total = total + i * weight(by)
        end do
    end function sum_loop

    !> A contained procedure called by another.
    integer function weight(step)
        integer, intent(in) :: step
        weight = step
    end function weight

    recursive integer(int64) function factorial(n) result(f)
        integer, intent(in) :: n
        if (n <= 1) then
            f = 1
        else
            f = n * factorial(n - 1)
        end if
    end function factorial

    !> Each element kept between low and high.
    elemental real(wp) function clamp(x, low, high)
        real(wp), intent(in) :: x, low, high
        clamp = min(max(x, low), high)
    end function clamp

    !> "55 (odd)": a number and whether it's even.
    function describe(n) result(text)
        integer, intent(in) :: n
        character(len=:), allocatable :: text
        character(len=20) :: digits
        write (digits, '(i0)') n
        text = trim(digits) // merge(" (even)", " (odd) ", mod(n, 2) == 0)
        text = trim(text)
    end function describe

    function complex_text(c) result(text)
        complex(wp), intent(in) :: c
        character(len=:), allocatable :: text
        character(len=32) :: buffer
        write (buffer, '("(",f0.1,",",f0.1,")")') c%re, c%im
        text = trim(buffer)
    end function complex_text

end program check_debugger
